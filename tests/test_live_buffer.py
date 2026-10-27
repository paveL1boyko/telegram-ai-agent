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
