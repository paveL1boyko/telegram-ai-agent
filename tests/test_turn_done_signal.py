"""Turn-completion ✅ signal.

Covers the two layers of the feature:

1. `tui/transcript.parse_transcript_event` synthesizes a bare `result`
   sentinel on assistant messages with ``stop_reason == "end_turn"`` — the
   CC TUI transcript has no `result` records, so end_turn is the only
   precise "turn fully done" marker (tool_use stops never trigger it,
   interrupts produce no end_turn so /cancel stays quiet);
2. the recovery-tail on_event factory posts ✅ on the sentinel — covers
   turns typed straight into the tmux pane and turns drained by recovery
   tails after a bot restart.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core.handlers.streaming import build_recovery_on_event_factory
from telegram_bot.core.messages import t
from telegram_bot.core.services.claude import StreamEvent
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.tui.transcript import parse_transcript_event


def _assistant_line(stop_reason: str | None, text: str = "answer") -> str:
    message: dict = {"content": [{"type": "text", "text": text}] if text else []}
    if stop_reason is not None:
        message["stop_reason"] = stop_reason
    return json.dumps({"type": "assistant", "message": message})


def test_end_turn_appends_result_sentinel_after_text() -> None:
    events, _ = parse_transcript_event(_assistant_line("end_turn"))
    assert [e.type for e in events] == ["text", "result"]
    assert events[0].content == "answer"
    assert events[1].content == ""


def test_tool_use_stop_has_no_sentinel() -> None:
    raw = json.dumps(
        {
            "type": "assistant",
            "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
            },
        }
    )
    events, _ = parse_transcript_event(raw)
    assert [e.type for e in events] == ["status"]


def test_end_turn_without_text_still_signals() -> None:
    events, _ = parse_transcript_event(_assistant_line("end_turn", text=""))
    assert [e.type for e in events] == ["result"]


def test_missing_stop_reason_has_no_sentinel() -> None:
    events, _ = parse_transcript_event(_assistant_line(None))
    assert [e.type for e in events] == ["text"]


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> SimpleNamespace:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=len(self.sent))


async def test_recovery_factory_posts_turn_done_marker(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(json.dumps({"topics": {"42": {"name": "x", "stream_mode": "live"}}}))
    bot = _FakeBot()
    factory = build_recovery_on_event_factory(
        bot=bot,  # type: ignore[arg-type]
        topic_config=TopicConfig(str(config_path)),
        session_manager=SimpleNamespace(record_message=lambda *a, **k: None),  # type: ignore[arg-type]
        tmux_manager=SimpleNamespace(  # type: ignore[arg-type]
            get_buffer=lambda key: None,
            get_session_snapshot=lambda key: None,
        ),
    )
    on_event = factory((100, 42))

    await on_event(StreamEvent("result", ""))

    # Not a bare ✅: Telegram renders emoji-only messages huge — the marker
    # always carries the localized word to stay a normal-sized line.
    assert [m["text"] for m in bot.sent] == [t("ui.turn_done")]
    assert bot.sent[0]["chat_id"] == 100
    assert bot.sent[0]["message_thread_id"] == 42
