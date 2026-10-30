"""Auto-compact watchdog: context size from the transcript → `/compact`.

Covers `tui/context_usage.read_context_usage` (newest assistant usage,
tail-only read) and `TmuxManager._check_channel_context` (threshold from
topic override or bot default, one compact per measurement, Claude-only).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_bot.core.services import tmux_manager as tmux_manager_module
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState
from telegram_bot.core.tui.context_usage import compact_boundary_since, read_context_usage

IDLE_PANE = "────────\n  ⏵⏵ bypass permissions on\n"
BUSY_PANE = "⏺ Working on it…\n  esc to interrupt\n"
COMPACT_BOUNDARY_LINE = json.dumps(
    {
        "type": "system",
        "subtype": "compact_boundary",
        "compactMetadata": {"preTokens": 230000, "postTokens": 12000},
    }
)


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


def test_compact_boundary_since_only_counts_lines_after_offset(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(COMPACT_BOUNDARY_LINE + "\n")
    offset = transcript.stat().st_size
    assert compact_boundary_since(transcript, 0) is True
    assert compact_boundary_since(transcript, offset) is False
    with transcript.open("a") as fh:
        fh.write(_user_line("x" * 600_000) + "\n")  # spans several scan chunks
        fh.write(COMPACT_BOUNDARY_LINE + "\n")
    assert compact_boundary_since(transcript, offset) is True
    assert compact_boundary_since(tmp_path / "missing.jsonl", 0) is False


# --- _check_channel_context -------------------------------------------------


def _manager(
    tmp_path: Path,
    *,
    default_tokens: int,
    override: int | None = None,
    continue_prompt: str = "continue",
) -> TmuxManager:
    mgr = TmuxManager(sessions_dir=tmp_path / "tmux_sessions")
    mgr._auto_compact_default_tokens = default_tokens
    mgr._auto_compact_continue_prompt = continue_prompt
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


def _wire(
    mgr: TmuxManager,
    tmp_path: Path,
    lines: list[str],
    provider: str = "claude",
    *,
    pane: str = IDLE_PANE,
    monkeypatch=None,
) -> tuple:
    key = (100, 42)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(lines) + "\n")
    mgr._sessions[key] = _state(tmp_path, provider)
    mgr._transcript_path_for_state = lambda state: transcript  # type: ignore[method-assign]
    sent: list[tuple[object, str, bool]] = []

    async def fake_send_direct(channel_key, prompt, *, set_processing=True):  # type: ignore[no-untyped-def]
        sent.append((channel_key, prompt, set_processing))
        return True

    async def fake_capture(session_name: str) -> str:
        return pane

    mgr.send_direct = fake_send_direct  # type: ignore[method-assign]
    if monkeypatch is not None:
        monkeypatch.setattr(tmux_manager_module, "capture_pane", fake_capture)
        monkeypatch.setattr(tmux_manager_module, "_AUTO_COMPACT_RESUME_SETTLE_SEC", 0)
    return key, transcript, sent


async def test_compacts_and_queues_continue_when_over_default_threshold(
    tmp_path: Path, monkeypatch
) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=230_000)], monkeypatch=monkeypatch
    )

    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
        await mgr._check_channel_context(key)  # same measurement — no second /compact

    # Both entries land back-to-back: /compact is a control command (no
    # processing flag), the continue right behind it is a real turn.
    assert sent == [(key, "/compact", False), (key, "continue", True)]

    # Still oversized lines keep arriving while the queued /compact waits
    # for the turn to end — they must not queue more compactions.
    with transcript.open("a") as fh:
        fh.write(_assistant_line("a2", cached=240_000) + "\n")
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert len(sent) == 2

    # Once the compaction lands, a fresh oversized turn re-arms the check.
    with transcript.open("a") as fh:
        fh.write(COMPACT_BOUNDARY_LINE + "\n")
        fh.write(_assistant_line("a3", cached=240_000) + "\n")
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert len(sent) == 4


async def test_pending_gate_expires_after_deadline(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=230_000)], monkeypatch=monkeypatch
    )
    monkeypatch.setattr(tmux_manager_module, "_AUTO_COMPACT_RESUME_DEADLINE_SEC", -1)
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
        with transcript.open("a") as fh:
            fh.write(_assistant_line("a2", cached=240_000) + "\n")
        await mgr._check_channel_context(key)
    assert len(sent) == 4


async def test_compaction_in_flight_in_pane_is_not_doubled(tmp_path: Path, monkeypatch) -> None:
    """Manual /compact already running (pane shows Compacting…): the stale
    pre-compact measurement must not trigger a second one."""
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr,
        tmp_path,
        [_assistant_line("a1", cached=230_000)],
        pane="✻ Compacting conversation… (esc to interrupt)\n",
        monkeypatch=monkeypatch,
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []
    assert key not in mgr._auto_compact_seen


def test_compact_boundary_supersedes_stale_usage(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _assistant_line("a1", cached=230_000) + "\n" + COMPACT_BOUNDARY_LINE + "\n"
    )
    usage = read_context_usage(transcript)
    assert usage is not None
    assert usage.tokens == 12_000
    assert usage.line_id.startswith("boundary:")


async def test_under_threshold_does_nothing(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=100_000)], monkeypatch=monkeypatch
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []


async def test_topic_override_wins_over_default(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000, override=50_000)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=60_000)], monkeypatch=monkeypatch
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == [(key, "/compact", False), (key, "continue", True)]
    assert mgr.auto_compact_threshold(key) == 50_000


async def test_topic_override_zero_disables(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000, override=0)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=900_000)], monkeypatch=monkeypatch
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []


async def test_non_claude_provider_is_skipped(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=1)
    key, _transcript, sent = _wire(
        mgr,
        tmp_path,
        [_assistant_line("a1", cached=900_000)],
        provider="codex",
        monkeypatch=monkeypatch,
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == []
    assert mgr.context_tokens(key) is None


def test_context_tokens_reports_measurement(tmp_path: Path) -> None:
    mgr = _manager(tmp_path, default_tokens=0)
    key, _transcript, _sent = _wire(mgr, tmp_path, [_assistant_line("a1", cached=12_345)])
    assert mgr.context_tokens(key) == 12_345


# --- continue queued behind the compact -------------------------------------


async def test_busy_session_queues_continue_immediately(tmp_path: Path, monkeypatch) -> None:
    """Compact fired mid-task (pane shows `esc to interrupt`): the continue
    prompt is typed right behind the compact instead of waiting for the
    compact_boundary, so CC's queue holds both entries."""
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr,
        tmp_path,
        [_assistant_line("a1", cached=230_000)],
        pane=BUSY_PANE,
        monkeypatch=monkeypatch,
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)

    assert sent == [(key, "/compact", False), (key, "continue", True)]


async def test_idle_session_queues_continue_too(tmp_path: Path, monkeypatch) -> None:
    """The 2026-10-30 regression: an idle session that crossed the threshold
    between turns was compacted but never resumed — the continue is now
    unconditional."""
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=230_000)], monkeypatch=monkeypatch
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert [p for _, p, _ in sent] == ["/compact", "continue"]


async def test_continue_disabled_by_empty_prompt(tmp_path: Path, monkeypatch) -> None:
    mgr = _manager(tmp_path, default_tokens=220_000, continue_prompt="")
    key, _transcript, sent = _wire(
        mgr,
        tmp_path,
        [_assistant_line("a1", cached=230_000)],
        pane=BUSY_PANE,
        monkeypatch=monkeypatch,
    )
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == [(key, "/compact", False)]


async def test_continue_skipped_when_compact_not_delivered(tmp_path: Path, monkeypatch) -> None:
    """A modal-blocked `/compact` already posted its alert; queueing the
    continue behind it would just bounce off the same modal."""
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=230_000)], monkeypatch=monkeypatch
    )

    async def failing_send_direct(channel_key, prompt, *, set_processing=True):  # type: ignore[no-untyped-def]
        sent.append((channel_key, prompt, set_processing))
        return False

    mgr.send_direct = failing_send_direct  # type: ignore[method-assign]
    with patch.object(mgr, "_tmux_alive", return_value=True):
        await mgr._check_channel_context(key)
    assert sent == [(key, "/compact", False)]


async def test_continue_aborts_when_session_replaced(tmp_path: Path, monkeypatch) -> None:
    """`/new` during the settle pause: the continue prompt must not land in
    the replacement session."""
    mgr = _manager(tmp_path, default_tokens=220_000)
    key, _transcript, sent = _wire(
        mgr, tmp_path, [_assistant_line("a1", cached=230_000)], monkeypatch=monkeypatch
    )
    real_sleep = asyncio.sleep

    async def swap_during_settle(delay: float) -> None:
        # The settle pause is the only sleep left in the check once
        # capture_pane and send_direct are stubbed.
        mgr._sessions[key] = _state(tmp_path)  # fresh state object
        await real_sleep(0)

    with (
        patch.object(mgr, "_tmux_alive", return_value=True),
        patch.object(tmux_manager_module.asyncio, "sleep", swap_during_settle),
    ):
        await mgr._check_channel_context(key)
    assert sent == [(key, "/compact", False)]
