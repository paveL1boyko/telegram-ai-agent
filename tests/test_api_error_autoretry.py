"""Auto-retry for transient Claude Code API errors (api_error StreamEvents).

Covers the three layers of the feature:

1. transcript parsing — `isApiErrorMessage` entries become `api_error`
   events instead of regular assistant text;
2. the retryable-pattern matcher and the consecutive-retry budget;
3. the TmuxManager interception — nudge delivery, budget exhaustion
   fallback to raw error text, and budget reset on forward progress.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services import tmux_manager as tmux_manager_module
from telegram_bot.core.services.api_error_retry import (
    ApiErrorRetryTracker,
    is_retryable_api_error,
)
from telegram_bot.core.services.claude import StreamEvent
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState
from telegram_bot.core.tui.transcript import parse_jsonl_line, parse_transcript_event

SIGNATURE_ERROR = "API Error: 400 messages.3.content.0: Invalid `signature` in `thinking` block"


def _api_error_line(text: str = SIGNATURE_ERROR) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {"content": [{"type": "text", "text": text}]},
            "sessionId": "11111111-2222-4333-8444-555555555555",
        }
    )


def test_parse_jsonl_line_flags_api_error() -> None:
    parsed = parse_jsonl_line(_api_error_line())
    assert parsed is not None
    assert parsed.kind == "api_error"
    assert parsed.payload["text"] == SIGNATURE_ERROR

    # String content form is accepted too.
    raw = json.dumps(
        {"type": "assistant", "isApiErrorMessage": True, "message": {"content": "API Error: x"}}
    )
    parsed = parse_jsonl_line(raw)
    assert parsed is not None
    assert parsed.kind == "api_error"
    assert parsed.payload["text"] == "API Error: x"

    # Flagged entry without extractable text collapses to skip, not text.
    raw = json.dumps({"type": "assistant", "isApiErrorMessage": True, "message": {"content": []}})
    parsed = parse_jsonl_line(raw)
    assert parsed is not None
    assert parsed.kind == "skip"


def test_parse_jsonl_line_regular_assistant_text_unaffected() -> None:
    raw = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
    )
    parsed = parse_jsonl_line(raw)
    assert parsed is not None
    assert parsed.kind == "text"
    assert parsed.payload["text"] == "hello"


def test_parse_transcript_event_emits_api_error_stream_event() -> None:
    events, session_id = parse_transcript_event(_api_error_line())
    assert [e.type for e in events] == ["api_error"]
    assert events[0].content == SIGNATURE_ERROR
    assert session_id == "11111111-2222-4333-8444-555555555555"


def test_is_retryable_api_error_patterns() -> None:
    assert is_retryable_api_error(SIGNATURE_ERROR)
    # Without backticks and with a proxy prefix.
    assert is_retryable_api_error(
        "API Error: 400 Upstream provider rejected the request. "
        "messages.1.content.7: Invalid signature in thinking block"
    )
    # Non-transient errors must keep surfacing to the user.
    assert not is_retryable_api_error(
        'API Error: Request rejected (429) · {"error":"rate limit exceeded"}'
    )
    assert not is_retryable_api_error("API Error: Can't reach the API server (ENOTFOUND)")
    assert not is_retryable_api_error("API Error: 400 Tool call 'x' has no tool result.")


def test_retry_tracker_budget_and_reset() -> None:
    tracker = ApiErrorRetryTracker(limit=2)
    key = (1, 2)
    assert tracker.register_error(key) == 1
    assert tracker.register_error(key) == 2
    assert tracker.register_error(key) is None
    tracker.note_progress(key)
    assert tracker.register_error(key) == 1

    disabled = ApiErrorRetryTracker(limit=0)
    assert disabled.register_error(key) is None


def test_settings_expose_api_error_retry_defaults() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    assert settings.api_error_retry_limit == 4
    assert settings.api_error_retry_prompt == "continue"


def _make_manager(tmp_path: Path, limit: int = 4) -> tuple[TmuxManager, TmuxSessionState]:
    manager = TmuxManager(
        sessions_dir=tmp_path,
        api_error_retry_limit=limit,
        api_error_retry_prompt="continue",
    )
    state = TmuxSessionState(
        session_name="cc-1-2",
        session_dir=str(tmp_path / "cc-1-2"),
        session_id="session-abc",
        mode="free",
        cwd=".",
        mcp_config="",
        chat_id=1,
    )
    manager._sessions[(1, 2)] = state
    return manager, state


def test_api_error_event_nudges_then_falls_back_to_raw_text(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux_manager_module, "_API_ERROR_NUDGE_DELAY_SEC", 0)
    manager, state = _make_manager(tmp_path, limit=2)
    key = (1, 2)

    sent_prompts: list[str] = []
    forwarded: list[StreamEvent] = []

    async def fake_safe_send(channel_key, st, prompt):
        sent_prompts.append(prompt)
        return True

    monkeypatch.setattr(manager, "_safe_send_and_enter", fake_safe_send)
    wrapped = manager._wrap_on_event_api_error_retry(key, state, forwarded.append)

    async def scenario() -> None:
        error = StreamEvent("api_error", SIGNATURE_ERROR)
        # Attempts 1 and 2: status line + "continue" nudge, no raw error text.
        await wrapped(error)
        await wrapped(error)
        assert sent_prompts == ["continue", "continue"]
        assert [e.type for e in forwarded] == ["status", "status"]
        assert "1/2" in forwarded[0].content and "2/2" in forwarded[1].content
        # Attempt 3: budget exhausted — raw error surfaces, no nudge.
        await wrapped(error)
        assert sent_prompts == ["continue", "continue"]
        assert forwarded[-1].type == "text"
        assert forwarded[-1].content == SIGNATURE_ERROR

    asyncio.run(scenario())


def test_api_error_budget_resets_on_forward_progress(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux_manager_module, "_API_ERROR_NUDGE_DELAY_SEC", 0)
    manager, state = _make_manager(tmp_path, limit=1)
    key = (1, 2)

    forwarded: list[StreamEvent] = []

    async def fake_safe_send(channel_key, st, prompt):
        return True

    monkeypatch.setattr(manager, "_safe_send_and_enter", fake_safe_send)
    wrapped = manager._wrap_on_event_api_error_retry(key, state, forwarded.append)

    async def scenario() -> None:
        error = StreamEvent("api_error", SIGNATURE_ERROR)
        await wrapped(error)  # attempt 1/1 — nudged
        # Model produced output afterwards — the incident healed.
        await wrapped(StreamEvent("text", "all good"))
        await wrapped(error)  # fresh budget — nudged again, not raw text
        assert [e.type for e in forwarded] == ["status", "text", "status"]

    asyncio.run(scenario())


def test_non_retryable_api_error_is_shown_without_nudge(tmp_path: Path, monkeypatch) -> None:
    manager, state = _make_manager(tmp_path)
    key = (1, 2)

    forwarded: list[StreamEvent] = []

    async def fail_if_called(channel_key, st, prompt):
        raise AssertionError("must not nudge on non-retryable errors")

    monkeypatch.setattr(manager, "_safe_send_and_enter", fail_if_called)
    wrapped = manager._wrap_on_event_api_error_retry(key, state, forwarded.append)

    rate_limited = 'API Error: Request rejected (429) · {"error":"rate limit exceeded"}'

    async def scenario() -> None:
        await wrapped(StreamEvent("api_error", rate_limited))
        assert [e.type for e in forwarded] == ["text"]
        assert forwarded[0].content == rate_limited

    asyncio.run(scenario())


def test_failed_nudge_delivery_still_surfaces_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tmux_manager_module, "_API_ERROR_NUDGE_DELAY_SEC", 0)
    manager, state = _make_manager(tmp_path)
    key = (1, 2)

    forwarded: list[StreamEvent] = []

    async def fake_safe_send(channel_key, st, prompt):
        return False  # modal blocked the pane — alert already posted

    monkeypatch.setattr(manager, "_safe_send_and_enter", fake_safe_send)
    wrapped = manager._wrap_on_event_api_error_retry(key, state, forwarded.append)

    async def scenario() -> None:
        await wrapped(StreamEvent("api_error", SIGNATURE_ERROR))
        assert [e.type for e in forwarded] == ["status", "text"]
        assert forwarded[-1].content == SIGNATURE_ERROR

    asyncio.run(scenario())
