"""Tests for the activity watchdog: tmux-typed output starts a recovery tail."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telegram_bot.core.services.claude import StreamEvent
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState


def _manager(tmp_path: Path) -> TmuxManager:
    return TmuxManager(sessions_dir=tmp_path / "tmux_sessions")


def _state(tmp_path: Path, *, offset: int) -> TmuxSessionState:
    return TmuxSessionState(
        session_name="cc-test",
        session_dir=str(tmp_path),
        session_id="sess-1",
        mode="free",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=100,
        offset=offset,
    )


def _patch_transcript(mgr: TmuxManager, path: Path) -> None:
    mgr._transcript_path_for_state = lambda state: path  # type: ignore[method-assign]


async def test_growth_starts_recovery_tail(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    key = (100, 42)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 100)
    mgr._sessions[key] = _state(tmp_path, offset=50)
    _patch_transcript(mgr, transcript)

    started: list[tuple[object, str]] = []

    async def fake_start(
        channel_key: object, state: object, output: object, *, reason: str
    ) -> bool:
        started.append((channel_key, reason))
        return True

    with (
        patch.object(mgr, "_tmux_alive", return_value=True),
        patch.object(mgr, "_start_recovery_tail", side_effect=fake_start),
    ):
        await mgr._check_channel_activity(key)

    assert started == [(key, "activity")]


async def test_no_growth_no_tail(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    key = (100, 42)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 100)
    mgr._sessions[key] = _state(tmp_path, offset=100)
    _patch_transcript(mgr, transcript)

    with (
        patch.object(mgr, "_tmux_alive", return_value=True),
        patch.object(mgr, "_start_recovery_tail") as start,
    ):
        await mgr._check_channel_activity(key)

    start.assert_not_called()


async def test_running_tail_not_replaced(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    key = (100, 42)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 100)
    mgr._sessions[key] = _state(tmp_path, offset=0)
    mgr._cancel_events[key] = asyncio.Event()
    _patch_transcript(mgr, transcript)

    with (
        patch.object(mgr, "_tmux_alive", return_value=True),
        patch.object(mgr, "_start_recovery_tail") as start,
    ):
        await mgr._check_channel_activity(key)

    start.assert_not_called()


async def test_missing_transcript_and_dead_tmux_are_skipped(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    key = (100, 42)
    mgr._sessions[key] = _state(tmp_path, offset=0)
    _patch_transcript(mgr, tmp_path / "missing.jsonl")

    with (
        patch.object(mgr, "_tmux_alive", return_value=False),
        patch.object(mgr, "_start_recovery_tail") as start,
    ):
        await mgr._check_channel_activity(key)
    start.assert_not_called()


async def test_start_activity_watchdog_is_idempotent(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.start_activity_watchdog(interval_sec=3600.0)
    first = mgr._activity_watchdog_task
    mgr.start_activity_watchdog(interval_sec=3600.0)
    assert mgr._activity_watchdog_task is first
    assert first is not None
    first.cancel()
    mgr._activity_watchdog_task = None


# --- Recovery on_event factory: tmux-typed output lands in the topic ---


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> SimpleNamespace:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))


class _SessionManagerStub:
    def __init__(self) -> None:
        self.recorded: list[tuple[int, str]] = []

    def record_message(
        self, message_id: int, session_id: str, *_args: object, **_kw: object
    ) -> None:
        self.recorded.append((message_id, session_id))


def _factory(
    tmp_path: Path,
    *,
    stream_mode: str,
    bot: _FakeBot,
    session_manager: _SessionManagerStub,
    tmux: object,
) -> Callable[[object], object]:
    import json

    from telegram_bot.core.handlers.streaming import build_recovery_on_event_factory
    from telegram_bot.core.services.topic_config import TopicConfig

    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps({"topics": {"42": {"name": "x", "stream_mode": stream_mode}}})
    )
    return build_recovery_on_event_factory(
        bot=bot,  # type: ignore[arg-type]
        topic_config=TopicConfig(str(config_path)),
        session_manager=session_manager,  # type: ignore[arg-type]
        tmux_manager=tmux,  # type: ignore[arg-type]
    )


async def test_recovery_factory_delivers_text_and_records(tmp_path: Path) -> None:
    bot = _FakeBot()
    session_manager = _SessionManagerStub()
    tmux = SimpleNamespace(
        get_buffer=lambda key: None,
        get_session_snapshot=lambda key: ("sid-1", "claude", None),
    )
    factory = _factory(
        tmp_path, stream_mode="verbose", bot=bot, session_manager=session_manager, tmux=tmux
    )
    on_event = factory((100, 42))

    await on_event(StreamEvent("text", "typed in tmux: **done**"))
    await on_event(StreamEvent("status", "⚙️ Running"))
    await on_event(StreamEvent("text", "   "))  # empty — dropped

    texts = [m["text"] for m in bot.sent]
    assert any("typed in tmux" in str(t) for t in texts)
    assert any("Running" in str(t) for t in texts)  # verbose → status delivered
    assert all(str(t).strip() for t in texts)
    assert bot.sent[0]["chat_id"] == 100 and bot.sent[0]["message_thread_id"] == 42
    assert session_manager.recorded == [(1, "sid-1")]


async def test_recovery_factory_minimal_drops_status(tmp_path: Path) -> None:
    bot = _FakeBot()
    session_manager = _SessionManagerStub()
    tmux = SimpleNamespace(
        get_buffer=lambda key: None,
        get_session_snapshot=lambda key: None,
    )
    factory = _factory(
        tmp_path, stream_mode="minimal", bot=bot, session_manager=session_manager, tmux=tmux
    )
    on_event = factory((100, 42))

    await on_event(StreamEvent("status", "⚙️ Running"))
    await on_event(StreamEvent("result_message", "final answer"))

    assert [m["text"] for m in bot.sent] == ["final answer"]
