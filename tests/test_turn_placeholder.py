"""Per-turn acknowledgement in tmux topics.

A plain prompt posts nothing when dispatched — the user just sent the
message, a "Thinking..." line under it was pure noise — and the live buffer
only appears with the first status event. Slash commands keep their
"Running /cmd..." acknowledgement because no ✅ turn-done marker follows
them.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.enums import ChatType

from telegram_bot.core.handlers.streaming import send_to_tmux_if_active
from telegram_bot.core.messages import t
from telegram_bot.core.services.live_buffer import LiveStatusBuffer


class _FakeTmuxManager:
    def __init__(self, *, stream_mode: str = "live") -> None:
        self.buffers: list[LiveStatusBuffer] = []
        self.sent: list[str] = []
        self.closed = 0
        self._topic_config = SimpleNamespace(
            get_topic=lambda thread_id: SimpleNamespace(stream_mode=stream_mode)
        )

    def is_active(self, key: object) -> bool:
        return True

    def is_tailing(self, key: object) -> bool:
        return True

    def get_topic_config(self) -> object:
        return self._topic_config

    def live_buffer_available(self) -> bool:
        return True

    def get_live_bot(self) -> object:
        return SimpleNamespace()

    async def set_buffer(self, key: object, buf: LiveStatusBuffer) -> None:
        self.buffers.append(buf)

    async def send_direct(self, key: object, prompt: str) -> bool:
        self.sent.append(prompt)
        return True

    async def close_buffer(self, key: object) -> None:
        self.closed += 1


class _FakeMessage:
    message_id = 5
    chat = SimpleNamespace(id=-100, type=ChatType.SUPERGROUP)

    def __init__(self) -> None:
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs: object) -> object:
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=900 + len(self.answers))


async def _close_all(mgr: _FakeTmuxManager) -> None:
    for buf in mgr.buffers:
        await buf.close()


async def test_plain_prompt_posts_no_placeholder() -> None:
    mgr = _FakeTmuxManager()
    msg = _FakeMessage()
    assert await send_to_tmux_if_active((-100, 42), "fix the tests", msg, mgr) is True  # type: ignore[arg-type]
    await _close_all(mgr)

    assert msg.answers == []
    assert mgr.sent == ["fix the tests"]
    # The buffer is wired but has no page yet — it posts with the first status line.
    assert len(mgr.buffers) == 1
    assert mgr.buffers[0].message_ids == []


async def test_slash_command_keeps_running_acknowledgement() -> None:
    mgr = _FakeTmuxManager()
    msg = _FakeMessage()
    await send_to_tmux_if_active((-100, 42), "/compact", msg, mgr)  # type: ignore[arg-type]
    await _close_all(mgr)

    assert [text for text, _ in msg.answers] == [t("ui.running_command", command="/compact")]
    assert msg.answers[0][1].get("reply_markup") is not None
    assert mgr.buffers[0].message_ids == [901]


async def test_minimal_mode_plain_prompt_is_silent() -> None:
    mgr = _FakeTmuxManager(stream_mode="minimal")
    msg = _FakeMessage()
    await send_to_tmux_if_active((-100, 42), "hello", msg, mgr)  # type: ignore[arg-type]
    assert msg.answers == []
    assert mgr.buffers == []
    assert mgr.sent == ["hello"]
