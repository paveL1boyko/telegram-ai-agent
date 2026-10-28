"""Auto-compact watchdog: context size from the transcript → `/compact`.

Covers `tui/context_usage.read_context_usage` (newest assistant usage,
tail-only read) and `TmuxManager._check_channel_context` (threshold from
topic override or bot default, one compact per measurement, Claude-only).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState
from telegram_bot.core.tui.context_usage import read_context_usage


def _assistant_line(uuid: str, *, cached: int, fresh: int = 0, created: int = 0) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid,
            "message": {
                "role": "assistant",
                "usage": {
                    "input_tokens": fresh,
                    "cache_creation_input_tokens": created,
                    "cache_read_input_tokens": cached,
                    "output_tokens": 50,
                },
            },
        }
    )


def _user_line(text: str = "hi") -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}})


# --- read_context_usage -----------------------------------------------------


def test_usage_sums_input_counters_of_newest_assistant_line(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _user_line(),
                _assistant_line("a1", cached=10_000),
                _user_line(),
                _assistant_line("a2", cached=161_317, created=3_028, fresh=5),
                _user_line("trailing user turn without usage"),
            ]
        )
        + "\n"
    )
    usage = read_context_usage(transcript)
    assert usage is not None
    assert usage.tokens == 161_317 + 3_028 + 5
    assert usage.line_id == "a2"


def test_usage_reads_only_the_tail_of_a_large_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    with transcript.open("w") as fh:
        fh.write(_assistant_line("old", cached=999_999) + "\n")
        fh.write(_user_line("x" * (2 * 1024 * 1024)) + "\n")  # pushes "old" out of the tail
        fh.write(_assistant_line("new", cached=1_000) + "\n")
    usage = read_context_usage(transcript)
    assert usage is not None
    assert usage.line_id == "new"
    assert usage.tokens == 1_000


def test_usage_none_when_missing_or_no_assistant_lines(tmp_path: Path) -> None:
    assert read_context_usage(tmp_path / "missing.jsonl") is None
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_user_line() + "\nnot json\n")
    assert read_context_usage(transcript) is None


# --- _check_channel_context -------------------------------------------------


def _manager(tmp_path: Path, *, default_tokens: int, override: int | None = None) -> TmuxManager:
    mgr = TmuxManager(sessions_dir=tmp_path / "tmux_sessions")
    mgr._auto_compact_default_tokens = default_tokens
    mgr._topic_config = SimpleNamespace(
        get_topic=lambda thread_id: SimpleNamespace(auto_compact_tokens=override)
    )
    return mgr


def _state(tmp_path: Path, provider: str = "claude") -> TmuxSessionState:
    return TmuxSessionState(
        session_name="cc-test",
        session_dir=str(tmp_path),
        session_id="sess-1",
        mode="free",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=100,
        provider=provider,
    )


def _wire(mgr: TmuxManager, tmp_path: Path, lines: list[str], provider: str = "claude") -> tuple:
    key = (100, 42)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    mgr._sessions[key] = _state(tmp_path, provider)
    mgr._transcript_path_for_state = lambda state: transcript  # type: ignore[method-assign]
    sent: list[tuple[object, str, bool]] = []

    async def fake_send_direct(channel_key, prompt, *, set_processing=True):  # type: ignore[no-untyped-def]
        sent.append((channel_key, prompt, set_processing))
        return True

    mgr.send_direct = fake_send_direct  # type: ignore[method-assign]
    return key, transcript, sent


async def test_compacts_once_when_over_default_threshold(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, transcript, sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=230_000)])

    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
        await mgr._check_channel_context(key)  # same measurement — no second /compact

    assert sent == [(key, "/compact", False)]

    # A new assistant turn that is still oversized re-arms the check.
    with transcript.open("a") as fh:
        fh.write(_assistant_line("a2", cached=240_000) + "\n")
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert len(sent) == 2


async def test_under_threshold_does_nothing(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=100_000)])
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []


async def test_topic_override_wins_over_default(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000, override=50_000)
    key, _transcript, sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=60_000)])
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == [(key, "/compact", False)]
    assert mgr.auto_compact_threshold(key) == 50_000


async def test_topic_override_zero_disables(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000, override=0)
    key, _transcript, sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=900_000)])
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []


async def test_non_claude_provider_is_skipped(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=1)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=900_000)], provider="codex"
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []
    assert mgr.context_tokens(key) is None


def test_context_tokens_reports_measurement(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=0)
    key, _transcript, _sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=12_345)])
    assert mgr.context_tokens(key) == 12_345
