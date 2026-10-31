"""LiveStatusBuffer rendering: expandable-blockquote history layout.

The buffer edits one Telegram message with the activity log. To keep the
log's visual footprint small (Telegram has no font-size entity), all
lines but the last are wrapped in an expandable blockquote; the latest
line stays outside so current activity is visible without expanding.
"""

from __future__ import annotations

import asyncio
import re

from telegram_bot.core.services.live_buffer import LiveStatusBuffer

_TS_PREFIX = re.compile(r"\[\d{2}:\d{2}:\d{2}\] ")


def _rendered(
    lines: list[str],
    *,
    header_text: str = "",
    continuation: bool = False,
) -> str:
    """Render `lines` through a real buffer (bot unused by _render)."""

    async def run() -> str:
        buf = LiveStatusBuffer(
            bot=None,  # type: ignore[arg-type]
            chat_id=1,
            thread_id=None,
            initial_message_id=1,
            header_text=header_text,
        )
        buf._is_continuation = continuation
        for line in lines:
            await buf.append(line)
        text = buf._render()
        # close() flushes via the (absent) bot — the failure is logged
        # inside close(), not raised.
        await buf.close()
        return text

    return asyncio.run(run())


def _strip_ts(text: str) -> str:
    return _TS_PREFIX.sub("", text)


def test_single_line_has_no_blockquote() -> None:
    text = _rendered(["✏️ Editing foo.py"])
    assert "<blockquote" not in text
    assert _strip_ts(text) == "✏️ Editing foo.py"


def test_history_collapses_latest_stays_outside() -> None:
    text = _rendered(["🔧 Read: a.py", "✏️ Editing b.py", "⚙️ Running: pytest"])
    assert _strip_ts(text) == (
        "<blockquote expandable>🔧 Read: a.py\n✏️ Editing b.py</blockquote>\n⚙️ Running: pytest"
    )


def test_two_lines_quote_only_the_first() -> None:
    text = _rendered(["one", "two"])
    assert _strip_ts(text) == "<blockquote expandable>one</blockquote>\ntwo"


def test_header_rendered_before_the_quote() -> None:
    text = _rendered(["one", "two"], header_text="⏳ Думаю…")
    assert _strip_ts(text).startswith("⏳ Думаю…\n<blockquote expandable>")


def test_continuation_header_replaces_header_text() -> None:
    text = _rendered(["one", "two"], header_text="⏳ Думаю…", continuation=True)
    assert _strip_ts(text).startswith("⏳ Думаю…\n<blockquote expandable>")
    # The original header_text must not leak into continuation pages.
    assert _strip_ts(text).count("⏳ Думаю…") == 1


def test_empty_buffer_renders_header_only() -> None:
    assert _rendered([], header_text="⏳ Думаю…") == "⏳ Думаю…\n"
    assert _rendered([]) == ""


# --- finalize (empty placeholder → completion marker) -----------------------


class _RecordingBot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str]] = []

    async def edit_message_text(self, *, text: str, message_id: int, **kwargs: object) -> None:
        self.edits.append((message_id, text))


def _make_buffer(bot: _RecordingBot, *, header_text: str = "⏳ Думаю…") -> LiveStatusBuffer:
    return LiveStatusBuffer(
        bot=bot,  # type: ignore[arg-type]
        chat_id=1,
        thread_id=None,
        initial_message_id=42,
        header_text=header_text,
    )


def test_finalize_turns_empty_placeholder_into_marker() -> None:
    async def run() -> tuple[_RecordingBot, LiveStatusBuffer, bool]:
        bot = _RecordingBot()
        buf = _make_buffer(bot)
        ok = await buf.finalize("✅ Done")
        return bot, buf, ok

    bot, buf, ok = asyncio.run(run())
    assert ok is True
    assert bot.edits[-1] == (42, "✅ Done")
    assert buf._closed  # worker stopped, later appends are ignored


def test_finalize_leaves_buffer_with_history_alone() -> None:
    async def run() -> tuple[_RecordingBot, bool]:
        bot = _RecordingBot()
        buf = _make_buffer(bot)
        await buf.append("🔧 Read: a.py")
        ok = await buf.finalize("✅ Done")
        await buf.close()
        return bot, ok

    bot, ok = asyncio.run(run())
    assert ok is False
    assert all(text != "✅ Done" for _, text in bot.edits)


def test_finalize_after_close_returns_false() -> None:
    async def run() -> tuple[_RecordingBot, bool]:
        bot = _RecordingBot()
        buf = _make_buffer(bot)
        await buf.close()
        return bot, await buf.finalize("✅ Done")

    bot, ok = asyncio.run(run())
    assert ok is False
    assert all(text != "✅ Done" for _, text in bot.edits)


# --- lazy first page (no "Thinking..." placeholder) -------------------------


class _SendingBot(_RecordingBot):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int | None, str]] = []
        self._next_id = 100

    async def send_message(
        self, *, chat_id: int, text: str, message_thread_id: int | None = None, **kwargs: object
    ) -> object:
        self.sent.append((message_thread_id, text))
        self._next_id += 1
        return type("Sent", (), {"message_id": self._next_id})()


def _lazy_buffer(bot: _SendingBot) -> LiveStatusBuffer:
    return LiveStatusBuffer(bot=bot, chat_id=1, thread_id=7, throttle_sec=0.01)  # type: ignore[arg-type]


def test_lazy_buffer_posts_nothing_without_lines() -> None:
    """A pure-text turn leaves no progress message behind at all."""

    async def run() -> tuple[_SendingBot, bool, list[int]]:
        bot = _SendingBot()
        buf = _lazy_buffer(bot)
        await asyncio.sleep(0.05)  # several worker ticks with nothing to show
        ok = await buf.finalize("✅ Done")  # nothing to finalize → caller sends the marker
        await buf.close()
        return bot, ok, buf.message_ids

    bot, ok, ids = asyncio.run(run())
    assert bot.sent == []
    assert bot.edits == []
    assert ok is False
    assert ids == []


def test_lazy_buffer_first_flush_posts_the_page_with_content() -> None:
    async def run() -> tuple[_SendingBot, list[int]]:
        bot = _SendingBot()
        buf = _lazy_buffer(bot)
        await buf.append("🔧 Read: a.py")
        await asyncio.sleep(0.05)
        await buf.append("✏️ Editing b.py")
        await buf.close()
        return bot, buf.message_ids

    bot, ids = asyncio.run(run())
    # One page, posted straight with the first line, then edited in place.
    assert len(bot.sent) == 1
    thread_id, first_text = bot.sent[0]
    assert thread_id == 7
    assert _strip_ts(first_text) == "🔧 Read: a.py"
    assert ids == [101]
    assert bot.edits and bot.edits[-1][0] == 101
    assert _strip_ts(bot.edits[-1][1]).endswith("✏️ Editing b.py")
