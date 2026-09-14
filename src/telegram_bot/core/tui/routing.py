"""Slash-command routing: bot-handled vs forwarded to tmux TUI.

Commands listed in `BOT_RESERVED_COMMANDS` are handled server-side and NOT
forwarded to the Claude CLI pane. Everything else (e.g. `/model sonnet`,
`/mcp`) goes to the TUI.

Note: `/stop` was removed per Decision 12 — use `/cancel` or the in-bot
Stop button instead. `/tui` is handled by tail.py's aiogram Command handler
before the text catch-all; it is intentionally not in BOT_RESERVED_COMMANDS.

The same holds for a bare `/model`: commands.py claims it (with no argument)
via an aiogram Command filter that runs before this routing, and shows the
topic's model picker. It stays out of BOT_RESERVED_COMMANDS on purpose —
this set matches on the first token only, so listing it here would also
swallow `/model sonnet`, which has no bot handler and must reach the TUI.
"""

from __future__ import annotations

from typing import Literal

BOT_RESERVED_COMMANDS = frozenset(
    {
        "/start",
        "/new",
        "/clear",
        "/uimode",
        "/engine",
        "/resume",
        "/kill",
        "/cancel",
        "/stream",
        "/language",
        "/day",
        "/tui",
        "/compact",
    }
)


def route_slash_command(text: str) -> Literal["bot", "tui"]:
    """Return "bot" if the text's first token is bot-reserved, else "tui".

    Only the FIRST whitespace-separated token is examined so that e.g.
    "/model sonnet" (TUI) doesn't accidentally match "/uimode" (bot).
    """
    first = text.split(maxsplit=1)[0] if text else ""
    return "bot" if first in BOT_RESERVED_COMMANDS else "tui"
