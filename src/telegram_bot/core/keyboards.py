"""Core keyboard layouts — topic and stream mode controls."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from telegram_bot.core.messages import t
from telegram_bot.core.services.resume_listing import SessionEntry

RESUME_PAGE_SIZE = 9
_RESUME_BUTTONS_PER_ROW = 3
CWD_PAGE_SIZE = 8


def topic_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard with new chat, cancel, model/TUI and compact/cwd shortcuts.
    Each button sends its i18n text (e.g. `t("ui.btn_tui")` → "TUI 🖥"); a
    `F.text == t("ui.btn_*")` handler listens for that exact text and runs
    the matching slash command (`/tui`, `/model`, `/compact`, bare `/cwd`),
    so the user gets the same result with one keyboard tap."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t("ui.btn_new_chat")),
                KeyboardButton(text=t("ui.btn_cancel")),
            ],
            [
                KeyboardButton(text=t("ui.btn_model")),
                KeyboardButton(text=t("ui.btn_tui")),
            ],
            [
                KeyboardButton(text=t("ui.btn_compact")),
                KeyboardButton(text=t("ui.btn_cwd")),
            ],
        ],
        is_persistent=True,
        resize_keyboard=True,
    )


def stream_mode_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    """Three-button picker for per-topic stream_mode.

    Marks the currently active mode with ✅ so the user can see what's on
    without reading the caption twice.
    """

    def _label(mode: str, text: str) -> str:
        return f"✅ {text}" if current == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_label("verbose", "📜 verbose"),
                    callback_data="stream_mode:verbose",
                ),
                InlineKeyboardButton(
                    text=_label("live", "🔄 live"),
                    callback_data="stream_mode:live",
                ),
                InlineKeyboardButton(
                    text=_label("minimal", "🤫 minimal"),
                    callback_data="stream_mode:minimal",
                ),
            ],
        ],
    )


def exec_mode_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    """Two-button picker for per-topic exec_mode.

    Marks the currently active mode with ✅ so the user can see what's on
    without reading the caption twice.
    """

    def _label(mode: str, text: str) -> str:
        return f"✅ {text}" if current == mode else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_label("subprocess", t("ui.exec_mode_label_subprocess")),
                    callback_data="exec_mode:subprocess",
                ),
                InlineKeyboardButton(
                    text=_label("tmux", t("ui.exec_mode_label_tmux")),
                    callback_data="exec_mode:tmux",
                ),
            ],
        ],
    )


def engine_keyboard(current_engine: str | None = None) -> InlineKeyboardMarkup:
    """Three-button picker for provider engine (Claude Code, Codex, Nessy)."""

    def _engine_label(engine: str, text: str) -> str:
        return f"✅ {text}" if current_engine == engine else text

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_engine_label("claude", "Claude Code"),
                    callback_data="engine:claude",
                ),
                InlineKeyboardButton(
                    text=_engine_label("nessy", "Nessy"),
                    callback_data="engine:nessy",
                ),
            ],
        ],
    )


def model_keyboard(
    presets: Sequence[str],
    current_model: str | None = None,
) -> InlineKeyboardMarkup:
    """Picker for the model override of the current topic.

    Buttons carry the preset index rather than the model id: ids like
    `openrouter/anthropic/claude-fable-5[1m]` overflow the 64-byte
    callback_data limit. The first row resets to the CLI default (whatever
    `~/.claude/settings.json` selects), which is the only way back to "no
    override" once one is pinned.
    """

    def _label(text: str, selected: bool) -> str:
        return f"✅ {text}" if selected else text

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_label(t("ui.model_default_label"), current_model is None),
                callback_data="model:default",
            ),
        ],
    ]
    for idx, preset in enumerate(presets):
        rows.append(
            [
                InlineKeyboardButton(
                    text=_label(preset, preset == current_model),
                    callback_data=f"model:{idx}",
                ),
            ],
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def resume_keyboard(
    entries: tuple[SessionEntry, ...] | list[SessionEntry],
    *,
    page: int,
    current_session_id: str | None,
    token: str,
) -> InlineKeyboardMarkup:
    """Inline picker for /resume sessions."""
    total_pages = max(1, (len(entries) + RESUME_PAGE_SIZE - 1) // RESUME_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * RESUME_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    _ = current_session_id
    end = min(start + RESUME_PAGE_SIZE, len(entries))
    row: list[InlineKeyboardButton] = []
    for idx in range(start, end):
        row.append(
            InlineKeyboardButton(
                text=str(idx + 1),
                callback_data=f"rs:s:{token}:{idx}",
            )
        )
        if len(row) == _RESUME_BUTTONS_PER_ROW:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append(
        [
            InlineKeyboardButton(text="◀", callback_data=f"rs:p:{token}:{max(page - 1, 0)}"),
            InlineKeyboardButton(
                text=f"Стр. {page + 1} из {total_pages}",
                callback_data=f"rs:p:{token}:{page}",
            ),
            InlineKeyboardButton(
                text="▶",
                callback_data=f"rs:p:{token}:{min(page + 1, total_pages - 1)}",
            ),
        ]
    )
    rows.append([InlineKeyboardButton(text="✕", callback_data=f"rs:cancel:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cwd_keyboard(
    dirs: Sequence[Path],
    *,
    page: int,
    list_dir: Path,
    current_cwd: Path | None,
    token: str,
) -> InlineKeyboardMarkup:
    """Inline picker for /cwd: subdirectories of `list_dir`, one per row.

    Buttons carry the picker token and a global index into the sorted dir
    list — paths themselves would overflow the 64-byte callback_data limit.
    The currently effective cwd is marked ✅; "✅ <dirname>" selects the
    listed directory itself, "⬆ .." re-lists its parent.
    """
    total_pages = max(1, (len(dirs) + CWD_PAGE_SIZE - 1) // CWD_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CWD_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, min(start + CWD_PAGE_SIZE, len(dirs))):
        path = dirs[idx]
        marker = "✅ " if current_cwd is not None and path == current_cwd else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}📁 {path.name}",
                    callback_data=f"cw:s:{token}:{idx}",
                ),
            ],
        )

    if total_pages > 1:
        rows.append(
            [
                InlineKeyboardButton(text="◀", callback_data=f"cw:p:{token}:{max(page - 1, 0)}"),
                InlineKeyboardButton(
                    text=f"{page + 1}/{total_pages}",
                    callback_data=f"cw:p:{token}:{page}",
                ),
                InlineKeyboardButton(
                    text="▶",
                    callback_data=f"cw:p:{token}:{min(page + 1, total_pages - 1)}",
                ),
            ],
        )

    select_label = list_dir.name or str(list_dir)
    nav_row = [
        InlineKeyboardButton(
            text=f"✅ {select_label[:32]}",
            callback_data=f"cw:ok:{token}",
        ),
    ]
    if list_dir.parent != list_dir:
        nav_row.insert(0, InlineKeyboardButton(text="⬆ ..", callback_data=f"cw:u:{token}"))
    rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="✕", callback_data=f"cw:x:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}K"
    return f"{size / (1024 * 1024):.1f}M"


def _format_age(mtime: float) -> str:
    import time

    age = max(0, int(time.time() - mtime))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    if age < 86400:
        return f"{age // 3600}h"
    return f"{age // 86400}d"
