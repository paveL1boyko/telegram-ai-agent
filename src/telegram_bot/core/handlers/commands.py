"""Command handlers for bot-owned slash commands except /tui."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import math
import os
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.types.inaccessible_message import InaccessibleMessage

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers._dispatch import enqueue_prompt
from telegram_bot.core.handlers.forward import ForwardBatcher
from telegram_bot.core.handlers.streaming import send_to_tmux_if_active
from telegram_bot.core.keyboards import (
    RESUME_PAGE_SIZE,
    _format_age,
    _format_size,
    cwd_keyboard,
    engine_keyboard,
    exec_mode_keyboard,
    model_keyboard,
    resume_keyboard,
    stream_mode_keyboard,
    topic_keyboard,
)
from telegram_bot.core.messages import reset_lang_cache, t
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.cwd_picker import (
    CwdPickerState,
    CwdPickerStore,
    list_subdirs,
)
from telegram_bot.core.services.message_queue import MessageQueue
from telegram_bot.core.services.picker_store import PickerState, PickerStore
from telegram_bot.core.services.providers import engine_display_name, get_adapter
from telegram_bot.core.services.resume_listing import (
    SessionEntry,
    _same_cwd,
    get_last_assistant_message,
    list_sessions,
)
from telegram_bot.core.services.telegram_utils import send_html_with_fallback
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.topic_config import (
    _VALID_ENGINES,
    _VALID_EXEC_MODES,
    _VALID_STREAM_MODES,
    TopicConfig,
    normalize_model_override,
)
from telegram_bot.core.services.topic_naming import sync_topic_name
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey, channel_key
from telegram_bot.core.utils.telegram_html import split_html_message

logger = logging.getLogger(__name__)


def _exec_mode_label(mode: str) -> str:
    """Human-facing label per exec_mode.

    Raw "subprocess" must never leak into the "UI mode: …" toast — the
    picker button text is the contract surface.
    """
    if mode == "subprocess":
        return t("ui.exec_mode_label_subprocess")
    if mode == "tmux":
        return t("ui.exec_mode_label_tmux")
    return mode


def _exec_mode_picker_caption(mode: str) -> str:
    return t("ui.exec_mode_picker_caption", current=_exec_mode_label(mode))


router = Router(name="commands")


def _resume_caption(
    cwd: Path,
    *,
    page: int,
    total_pages: int,
    entries: tuple[SessionEntry, ...] = (),
    current_session_id: str | None = None,
) -> str:
    safe_cwd = html.escape(str(cwd))
    text = t("ui.resume_picker_caption_hdr", cwd=safe_cwd, page=page + 1, total=total_pages)
    if not entries:
        return text

    blocks: list[str] = []
    start = page * RESUME_PAGE_SIZE
    for idx, entry in enumerate(entries[start : start + RESUME_PAGE_SIZE], start=start):
        provider = engine_display_name(entry.provider)
        preview = html.escape(entry.preview)
        prefix = "✅ " if entry.session_id == current_session_id else ""
        parts = [
            f"{prefix}{idx + 1}. <b>{provider}</b>",
            _format_age(entry.mtime),
            _format_size(entry.size_bytes),
            f"<code>{html.escape(entry.session_id[:8])}</code>",
        ]
        if entry.session_id == current_session_id:
            parts.append(t("ui.resume_current_marker"))
        block_lines = [" · ".join(parts)]
        if preview:
            block_lines.append(f"   {preview}")
        blocks.append("\n".join(block_lines))
    return "\n\n".join([text, *blocks])


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    logger.debug("User %s started the bot", message.from_user and message.from_user.id)
    is_group = message.chat.type == ChatType.SUPERGROUP
    keyboard = topic_keyboard() if is_group else ReplyKeyboardRemove()
    await message.answer(
        text=t("ui.start_welcome"),
        reply_markup=keyboard,
    )


@router.message(Command("language"))
async def handle_language(message: Message) -> None:
    """Show or switch bot UI language for the current process."""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    current = os.environ.get("BOT_LANG", "en")
    if current not in {"en", "ru"}:
        current = "en"

    if len(parts) == 1:
        await message.answer(t("ui.language_current", lang=current))
        return

    lang = parts[1].strip().lower()
    if lang not in {"en", "ru"}:
        await message.answer(t("ui.language_invalid"))
        return

    os.environ["BOT_LANG"] = lang
    reset_lang_cache()
    await message.answer(t("ui.language_changed", lang=lang))


async def _reset_channel(
    message: Message,
    key: ChannelKey,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Unified reset path for /new, /clear, and the "Новый чат" reply button.

    Live tmux → clear_context respawns a fresh TUI immediately.
    Dormant tmux → drop stale state and start a fresh TUI immediately.
    Otherwise → full subprocess reset + ui.new_session.
    """
    is_group = message.chat.type == ChatType.SUPERGROUP
    reply_kb = topic_keyboard() if is_group else None
    settings = topic_config.get_topic(key[1])
    if tmux_manager.is_active(key):
        # clear_context respawns the tmux session; _spawn_tmux can fail
        # (tmux server shutdown race, readiness timeout, etc.). Without a
        # catch here the RuntimeError reaches aiogram's error middleware
        # and the user sees nothing — "Новый чат" becomes a silent button.
        try:
            reset_live = await tmux_manager.clear_context(key, session_manager)
        except RuntimeError:
            logger.warning("clear_context failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"), reply_markup=reply_kb)
            return
        if reset_live:
            session = session_manager._get_session(key)
            await message.answer(
                t(
                    "ui.tmux_started_engine",
                    engine=engine_display_name(session.engine),
                    model=_model_label(session.model),
                ),
                reply_markup=reply_kb,
            )
            return
        logger.info("clear_context found no live tmux for %s; starting fresh", key)

    if settings.exec_mode == "tmux":
        await tmux_manager.kill(key)
        forward_batcher.clear(key)
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        session = session_manager._get_session(key)
        try:
            await tmux_manager.start_session(
                key,
                mode=session.mode,
                cwd=session.cwd,
                mcp_config=session.mcp_config,
                chat_id=session.chat_id,
                session_manager=session_manager,
                provider=session.engine,
                model=session.model,
            )
        except RuntimeError:
            logger.warning("fresh tmux start failed for %s", key, exc_info=True)
            await message.answer(t("ui.reset_failed"), reply_markup=reply_kb)
            return
        await message.answer(
            t(
                "ui.tmux_started_engine",
                engine=engine_display_name(session.engine),
                model=_model_label(session.model),
            ),
            reply_markup=reply_kb,
        )
        return

    forward_batcher.clear(key)
    await message_queue.clear(key)
    await session_manager.kill_session(key)
    await message.answer(t("ui.new_session"), reply_markup=reply_kb)


@router.message(Command("new"))
async def handle_new(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested new session", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("clear"))
async def handle_clear(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    logger.debug("User %s requested clear", message.from_user and message.from_user.id)
    await _reset_channel(
        message, key, session_manager, message_queue, forward_batcher, tmux_manager, topic_config
    )


@router.message(Command("cancel"))
async def handle_cancel_command(
    message: Message,
    session_manager: SessionManager,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
) -> None:
    key = channel_key(message)
    tmux_acted = tmux_manager.is_active(key)
    if tmux_acted:
        await tmux_manager.cancel(key)
    cancelled = await message_queue.cancel(key)
    if cancelled or tmux_acted:
        logger.debug("User cancelled CC processing (command) for %s", key)
        await message.answer(t("ui.cancelled"))
    else:
        await message.answer(t("ui.nothing_to_cancel"))


@router.message(Command("kill"))
async def handle_kill(message: Message, tmux_manager: TmuxManager) -> None:
    """Kill the tmux session in the current topic."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tmux_not_active"))
        return
    logger.debug(
        "User %s killed tmux session for %s", message.from_user and message.from_user.id, key
    )
    await tmux_manager.kill(key)
    await message.answer(t("ui.tmux_killed"))


@router.message(Command("compact"))
async def handle_compact_command(
    message: Message,
    message_queue: MessageQueue,
    tmux_manager: TmuxManager,
    topic_config: TopicConfig,
) -> None:
    """Compact the live tmux session context via the engine's native command.

    `/compact` compresses conversation context without losing the logical
    thread. It only makes sense for a persistent tmux session — subprocess
    mode spawns a fresh process per message, so there is no context to
    compact. The engine-specific slash command (/compact for Claude Code and
    Codex, /compress for Nessy) is delivered to the running TUI; the
    "Compacting…" / "Compacted N → M tokens" feedback comes back through the
    transcript tail.
    """
    key = channel_key(message)
    settings = topic_config.get_topic(key[1])
    if settings.exec_mode != "tmux":
        await message.answer(t("ui.compact_tmux_only"))
        return
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tmux_not_active"))
        return
    command = get_adapter(settings.engine).compact_slash_command()
    # Fast path: a tail is already running, so the command lands in the live
    # TUI and the compact feedback streams back immediately.
    if await send_to_tmux_if_active(key, command, message, tmux_manager):
        return
    # Session alive but no tail (e.g. right after /cancel): route through the
    # queue so send_stream starts a tail that captures the compact output.
    enqueue_prompt(
        key,
        command,
        message,
        message_queue,
        tmux_manager,
        target_session_id=None,
        inject_reply_if_no_target=False,
    )


@router.message(Command("reconnect"))
async def handle_reconnect(message: Message, tmux_manager: TmuxManager) -> None:
    """Restart the transcript tail without touching the tmux session or Claude context."""
    key = channel_key(message)
    if not tmux_manager.is_active(key):
        await message.answer(t("ui.tmux_not_active"))
        return
    result = await tmux_manager.reconnect_tail(key)
    if result == "started":
        await message.answer(t("ui.reconnect_started"))
    elif result == "needs_first_message":
        await message.answer(t("ui.reconnect_needs_first_message"))
    else:
        await message.answer(t("ui.reconnect_no_transcript"))


@router.message(Command("resume"))
async def handle_resume(
    message: Message,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    """Open server-side picker with resumable Claude/Codex sessions."""
    key = channel_key(message)
    is_group = message.chat.type == ChatType.SUPERGROUP
    reply_kb = topic_keyboard() if is_group else None
    if key[1] is None:
        await message.answer(t("ui.resume_not_in_forum"), reply_markup=reply_kb)
        return

    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    entries = tuple(await asyncio.to_thread(list_sessions, runtime.cwd))
    if not entries:
        await message.answer(t("ui.resume_no_sessions"), reply_markup=reply_kb)
        return

    token = picker_store.put(
        PickerState(
            chat_id=key[0],
            thread_id=key[1],
            cwd=runtime.cwd,
            engine=runtime.engine,
            entries=entries,
            created_at=time.time(),
        )
    )
    total_pages = max(1, math.ceil(len(entries) / 8))
    current_session_id = tmux_manager.get_active_session_id(key)
    await message.answer(
        _resume_caption(
            runtime.cwd,
            page=0,
            total_pages=total_pages,
            entries=entries,
            current_session_id=current_session_id,
        ),
        reply_markup=resume_keyboard(
            entries,
            page=0,
            current_session_id=current_session_id,
            token=token,
        ),
        parse_mode="HTML",
    )


def _callback_key(callback: CallbackQuery) -> ChannelKey | None:
    if callback.message is None or isinstance(callback.message, InaccessibleMessage):
        return None
    return (callback.message.chat.id, callback.message.message_thread_id)


async def _stale_resume_picker(callback: CallbackQuery) -> None:
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_picker_stale"), reply_markup=None)
    await callback.answer(t("ui.resume_picker_stale"), show_alert=True)


async def _answer_callback_safely(
    callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False
) -> None:
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer(text, show_alert=show_alert)


async def _replay_last_assistant_message(
    message: Message,
    entry: SessionEntry,
    key: ChannelKey,
    session_manager: SessionManager,
) -> None:
    content = await asyncio.to_thread(
        get_last_assistant_message,
        entry.provider,
        entry.transcript_path,
    )
    if not content:
        return

    is_group = message.chat.type == ChatType.SUPERGROUP
    reply_kb = topic_keyboard() if is_group else None
    chunks = split_html_message(content)
    for i, chunk in enumerate(chunks):
        kb: ReplyKeyboardMarkup | None = reply_kb if i == len(chunks) - 1 else None

        async def _send_html(c: str = chunk, k: ReplyKeyboardMarkup | None = kb) -> object:
            return await message.answer(c, parse_mode="HTML", reply_markup=k)

        async def _send_plain(c: str = chunk, k: ReplyKeyboardMarkup | None = kb) -> object:
            return await message.answer(c, reply_markup=k)

        outcome = await send_html_with_fallback(
            send_html=_send_html,
            send_plain=_send_plain,
            label=f"resume replay {key}",
        )
        if outcome.message_id is not None:
            session_manager.record_message(
                outcome.message_id,
                entry.session_id,
                key,
                provider=entry.provider,
                model=None,
            )
        if outcome.fatal:
            return


@router.callback_query(F.data.startswith("rs:p:"))
async def on_resume_page(
    callback: CallbackQuery,
    picker_store: PickerStore,
    tmux_manager: TmuxManager,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_page = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    try:
        page = int(raw_page)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    total_pages = max(1, math.ceil(len(state.entries) / 8))
    page = max(0, min(page, total_pages - 1))
    try:
        await callback.message.edit_text(
            _resume_caption(
                state.cwd,
                page=page,
                total_pages=total_pages,
                entries=state.entries,
                current_session_id=tmux_manager.get_active_session_id(key),
            ),
            reply_markup=resume_keyboard(
                state.entries,
                page=page,
                current_session_id=tmux_manager.get_active_session_id(key),
                token=token,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("rs:s:"))
async def on_resume_pick(
    callback: CallbackQuery,
    session_manager: SessionManager,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    picker_store: PickerStore,
    bot_defaults: BotDefaults,
) -> None:
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await _stale_resume_picker(callback)
        return
    _, _, token, raw_idx = parts
    state = picker_store.get(token)
    key = _callback_key(callback)
    if state is None or key != (state.chat_id, state.thread_id):
        await _stale_resume_picker(callback)
        return
    runtime = resolve_topic_runtime_config(topic_config.get_topic(key[1]), bot_defaults)
    if not _same_cwd(runtime.cwd, state.cwd):
        await _stale_resume_picker(callback)
        return
    try:
        idx = int(raw_idx)
    except ValueError:
        await _stale_resume_picker(callback)
        return
    if idx < 0:
        await _stale_resume_picker(callback)
        return
    try:
        entry = state.entries[idx]
    except IndexError:
        await _stale_resume_picker(callback)
        return

    await _answer_callback_safely(callback, t("ui.resume_starting"))
    result = await tmux_manager.switch_or_start_session(
        key,
        entry.session_id,
        entry.provider,
        entry.transcript_path,
        session_manager=session_manager,
        topic_config=topic_config,
        defaults=bot_defaults,
    )
    if result.kind == "target_missing":
        await callback.message.edit_text(t("ui.resume_target_missing"), reply_markup=None)
        return
    if result.kind in {"invalid_id", "spawn_failed", "config_write_failed"}:
        key_name = (
            "ui.resume_spawn_failed_engine_changed"
            if result.kind == "spawn_failed" and result.engine_changed
            else f"ui.resume_{result.kind}"
        )
        await callback.message.edit_text(
            t(key_name, engine=entry.provider),
            reply_markup=None,
        )
        return

    picker_store.drop(token)
    if result.kind == "already_on_it":
        await callback.message.edit_text(t("ui.resume_already_on_it"), reply_markup=None)
        await _replay_last_assistant_message(callback.message, entry, key, session_manager)
        return

    message_key = "ui.resume_switched" if result.kind == "switched" else "ui.resume_started"
    text = t(message_key, sid=entry.session_id[:8], model=_model_label(result.model))
    if result.engine_changed:
        text += "\n" + t("ui.resume_engine_switched", engine=entry.provider)
    await callback.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    await _replay_last_assistant_message(callback.message, entry, key, session_manager)


@router.callback_query(F.data.startswith("rs:cancel:"))
async def on_resume_cancel(callback: CallbackQuery, picker_store: PickerStore) -> None:
    if callback.data is not None:
        picker_store.drop(callback.data.rsplit(":", 1)[-1])
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(t("ui.resume_cancelled"), reply_markup=None)
    await callback.answer()


@router.message(Command("stream"))
async def handle_stream_mode(message: Message, topic_config: TopicConfig) -> None:
    """Show a 3-button picker to switch stream_mode for the current topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.stream_mode_not_in_forum"))
        return
    current = topic_config.get_topic(thread_id).stream_mode
    await message.answer(
        t("ui.stream_mode_picker_caption", current=current),
        reply_markup=stream_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("stream_mode:"))
async def on_stream_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
) -> None:
    """Apply a new stream_mode for the topic the picker was posted in."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id/edit methods — bail out if the
    # picker message is no longer reachable (e.g. deleted, chat lost).
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, mode = callback.data.partition(":")
    if mode not in _VALID_STREAM_MODES:
        await callback.answer(t("ui.stream_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(
            t("ui.stream_mode_not_in_forum"),
            show_alert=True,
        )
        return

    ok = await topic_config.update_stream_mode(thread_id, mode)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.stream_mode_write_failed"), show_alert=True)
        return

    # Picker self-destructs after a choice — like /model, no dead panels in history.
    await _close_picker(callback)
    await callback.answer(t("ui.stream_mode_changed", mode=mode))


@router.message(Command("uimode"))
async def handle_uimode_command(message: Message, topic_config: TopicConfig) -> None:
    """Show a 2-button picker to switch exec_mode for the current topic.

    Named `/uimode` (not `/mode`) so it does not collide with `/model` in
    users' heads; internally the setting is still `exec_mode`.
    """
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.exec_mode_not_in_forum"))
        return
    current = topic_config.get_topic(thread_id).exec_mode
    await message.answer(
        _exec_mode_picker_caption(current),
        reply_markup=exec_mode_keyboard(current),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("exec_mode:"))
async def on_exec_mode_click(
    callback: CallbackQuery,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
) -> None:
    """Apply a new exec_mode for the topic the picker was posted in.

    Order matters: busy-check precedes any side-effect, and tmux.kill strictly
    precedes the config write on tmux→subprocess (Decision 2 — if we wrote
    first and crashed, the next message would race a still-running tmux
    session against a fresh subprocess under the new mode).
    """
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    # InaccessibleMessage has no thread_id / edit methods — bail out if the
    # picker message is no longer reachable.
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, new_mode = callback.data.partition(":")
    # Re-validate against the whitelist even though the keyboard only emits
    # two canonical values — raw callback.data is user-controlled.
    if new_mode not in _VALID_EXEC_MODES:
        await callback.answer(t("ui.exec_mode_invalid"), show_alert=True)
        return

    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.exec_mode_not_in_forum"), show_alert=True)
        return

    key = (callback.message.chat.id, thread_id)
    previous_mode = topic_config.get_topic(thread_id).exec_mode

    if new_mode == previous_mode:
        await _close_picker(callback)
        await callback.answer(t("ui.exec_mode_already", mode=_exec_mode_label(new_mode)))
        return

    # Busy-check covers both channels: tmux's own processing flag AND the
    # subprocess-path MessageQueue (lock held OR items pending). Either way
    # we refuse the switch without touching tmux state.
    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    # tmux→subprocess: kill first, then persist. Reverse order leaves an
    # orphan tmux session if the write fails.
    if previous_mode == "tmux" and new_mode == "subprocess":
        await tmux_manager.kill(key)

    ok = await topic_config.update_exec_mode(thread_id, new_mode)
    if not ok:
        await callback.answer(t("ui.exec_mode_write_failed"), show_alert=True)
        return

    user_id = callback.from_user.id if callback.from_user else None
    logger.info(
        "exec_mode switched: user_id=%s thread_id=%s previous_mode=%s new_mode=%s",
        user_id,
        thread_id,
        previous_mode,
        new_mode,
    )

    # Picker self-destructs after a choice — like /model, no dead panels in history.
    await _close_picker(callback)
    await callback.answer(t("ui.exec_mode_changed", mode=_exec_mode_label(new_mode)))


@router.message(Command("engine"))
async def handle_engine_command(message: Message, topic_config: TopicConfig) -> None:
    """Show provider engine picker for the current forum topic."""
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.engine_not_in_forum"))
        return
    settings = topic_config.get_topic(thread_id)
    await message.answer(
        t(
            "ui.engine_picker_caption",
            engine=engine_display_name(settings.engine),
        ),
        reply_markup=engine_keyboard(settings.engine),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("engine:"))
async def on_engine_click(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
    bot_defaults: BotDefaults,
) -> None:
    """Apply provider engine changes for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, raw_value = callback.data.partition(":")
    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.engine_not_in_forum"), show_alert=True)
        return
    key = (callback.message.chat.id, thread_id)
    current = topic_config.get_topic(thread_id)

    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    if raw_value not in _VALID_ENGINES:
        await callback.answer(t("ui.engine_invalid"), show_alert=True)
        return
    new_engine = raw_value

    if new_engine == current.engine:
        await _close_picker(callback)
        await callback.answer(t("ui.engine_already"))
        return

    ok = await topic_config.update_engine_model(thread_id, new_engine, None)  # type: ignore[arg-type]
    if not ok:
        await callback.answer(t("ui.engine_write_failed"), show_alert=True)
        return

    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    await session_manager.clear_provider_session(key)

    # Engine switch resets the model override — refresh the topic name suffix.
    if settings.topic_autoname:
        await sync_topic_name(
            bot,
            chat_id=key[0],
            thread_id=thread_id,
            topic_config=topic_config,
            defaults=bot_defaults,
        )

    logger.info(
        "engine switched: user_id=%s thread_id=%s previous=%s new=%s model=%s",
        callback.from_user.id if callback.from_user else None,
        thread_id,
        current.engine,
        new_engine,
        current.model,
    )
    engine_name = engine_display_name(new_engine)
    await _close_picker(callback)
    await callback.answer(t("ui.engine_changed", engine=engine_name))
    await callback.message.answer(t("ui.engine_changed_new_session", engine=engine_name))


def _model_label(model: str | None) -> str:
    """Human-facing name for a model override; `None` means the CLI default."""
    return model if model else t("ui.model_default_label")


def _model_picker_caption(model: str | None) -> str:
    return t("ui.model_picker_caption", model=html.escape(_model_label(model)))


async def _close_picker(callback: CallbackQuery) -> None:
    """Delete the picker message once a choice has been made.

    Deletion can fail (message older than 48h, missing delete rights), so the
    fallback strips the keyboard instead, leaving a dead picker unclickable.
    Shared by every settings picker (model/engine/uimode/stream) — the user
    asked for pickers to self-destruct after a choice so they don't pile up
    in the topic history.
    """
    message = callback.message
    if message is None or isinstance(message, InaccessibleMessage):
        return
    try:
        await message.delete()
    except Exception:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except Exception:
            logger.debug("Failed to close model picker", exc_info=True)


async def _show_model_picker(
    message: Message,
    topic_config: TopicConfig,
    settings: Settings,
) -> None:
    """Post the model-override picker for the message's forum topic.

    Shared by the bare `/model` command and the reply-keyboard Model button.
    """
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.model_not_in_forum"))
        return
    presets = settings.model_presets
    if not presets:
        await message.answer(t("ui.model_no_presets"))
        return
    current = topic_config.get_topic(thread_id)
    await message.answer(
        _model_picker_caption(current.model),
        reply_markup=model_keyboard(presets, current.model),
        parse_mode="HTML",
    )


@router.message(Command("model", magic=F.args.is_(None)))
async def handle_model_command(
    message: Message,
    topic_config: TopicConfig,
    settings: Settings,
) -> None:
    """Show the model-override picker for the current forum topic.

    Only the bare `/model` is claimed here: `/model sonnet` keeps falling
    through to the text handler and reaches the CLI's own dialog in the TUI
    pane, as documented in `core.tui.routing`.
    """
    await _show_model_picker(message, topic_config, settings)


@router.message(F.text == t("ui.btn_model"))
async def handle_model_button(
    message: Message,
    topic_config: TopicConfig,
    settings: Settings,
) -> None:
    """Reply-keyboard Model button — same as bare /model."""
    await _show_model_picker(message, topic_config, settings)


@router.callback_query(F.data.startswith("model:"))
async def on_model_click(
    callback: CallbackQuery,
    bot: Bot,
    topic_config: TopicConfig,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    session_manager: SessionManager,
    settings: Settings,
    bot_defaults: BotDefaults,
) -> None:
    """Pin (or clear) the model override for the picker topic."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    _, _, raw_value = callback.data.partition(":")
    thread_id = callback.message.message_thread_id
    if thread_id is None:
        await callback.answer(t("ui.model_not_in_forum"), show_alert=True)
        return
    key = (callback.message.chat.id, thread_id)
    current = topic_config.get_topic(thread_id)

    if tmux_manager.is_processing(key) or message_queue.is_busy(key):
        await callback.answer(t("ui.exec_mode_busy"), show_alert=True)
        return

    presets = settings.model_presets
    if raw_value == "default":
        new_model: str | None = None
    else:
        # The keyboard may predate a MODEL_PRESETS edit, so the index is
        # re-checked against the live list instead of trusted.
        if not raw_value.isdigit() or int(raw_value) >= len(presets):
            await callback.answer(t("ui.model_invalid"), show_alert=True)
            return
        new_model = normalize_model_override(presets[int(raw_value)])
        if new_model is None:
            logger.warning("Rejected model preset %r", presets[int(raw_value)])
            await callback.answer(t("ui.model_invalid"), show_alert=True)
            return

    if new_model == current.model:
        await _close_picker(callback)
        await callback.answer(t("ui.model_already"))
        return

    ok = await topic_config.update_engine_model(thread_id, current.engine, new_model)
    if not ok:
        await callback.answer(t("ui.model_write_failed"), show_alert=True)
        return

    if settings.topic_autoname:
        await sync_topic_name(
            bot,
            chat_id=key[0],
            thread_id=thread_id,
            topic_config=topic_config,
            defaults=bot_defaults,
        )

    label = _model_label(new_model)

    # Prefer a live switch: send `/model <id>` straight into the running TUI
    # pane — the engine switches the model inside the current session, context
    # intact, no respawn. The busy-check above guarantees the session is idle,
    # so the command lands in the input bar, not mid-turn. set_processing=False:
    # a model switch is not a processing turn (no result event would ever
    # clear the flag).
    if tmux_manager.is_active(key):
        live_command = get_adapter(current.engine).live_model_command(new_model)
        if live_command is not None:
            if await tmux_manager.send_direct(key, live_command, set_processing=False):
                tmux_manager.update_session_model(key, new_model)
                logger.info(
                    "model switched live: user_id=%s thread_id=%s engine=%s previous=%s new=%s",
                    callback.from_user.id if callback.from_user else None,
                    thread_id,
                    current.engine,
                    current.model,
                    new_model,
                )
                await _close_picker(callback)
                await callback.answer(t("ui.model_changed", model=label))
                await callback.message.answer(t("ui.model_changed_live", model=label))
                return
            # Modal-blocked or tmux failure — the alert is already posted.
            # The override is persisted above, so it applies on the next
            # session start; the live session keeps its model and context.
            logger.info(
                "model live switch blocked: thread_id=%s engine=%s new=%s",
                thread_id,
                current.engine,
                new_model,
            )
            await _close_picker(callback)
            await callback.answer(t("ui.model_changed", model=label))
            await callback.message.answer(t("ui.model_live_failed", model=label))
            return

    # Respawn path: `--model` is a launch flag, and engines without a live
    # switch (Codex) or topics without a live session only pick the model
    # up on the next start. The session is dropped rather than leaving the
    # topic claiming a model it is not actually running.
    if tmux_manager.is_active(key):
        await tmux_manager.kill(key)
    await session_manager.clear_provider_session(key)

    logger.info(
        "model switched: user_id=%s thread_id=%s engine=%s previous=%s new=%s",
        callback.from_user.id if callback.from_user else None,
        thread_id,
        current.engine,
        current.model,
        new_model,
    )
    await _close_picker(callback)
    await callback.answer(t("ui.model_changed", model=label))
    await callback.message.answer(t("ui.model_changed_new_session", model=label))


_SENDFILE_MAX_BYTES = 50 * 1024 * 1024  # Telegram Bot API upload limit


def _sendfile_split_args(args: str) -> tuple[str, str]:
    """Split `/sendfile` args into (path, caption).

    A path containing spaces is only recognized when the whole argument
    string is an existing file; otherwise the first whitespace separates
    the path from the caption.
    """
    if Path(args).expanduser().is_file():
        return args, ""
    path, _, caption = args.partition(" ")
    return path, caption.strip()


def _sendfile_error(raw_path: str) -> tuple[str, dict[str, object]] | None:
    """Validation error for `/sendfile` as (message key, format args), or None."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return "ui.sendfile_not_absolute", {}
    if not path.is_file():
        return "ui.sendfile_not_found", {"path": raw_path}
    size = path.stat().st_size
    if size > _SENDFILE_MAX_BYTES:
        return "ui.sendfile_too_big", {"size": size // (1024 * 1024)}
    return None


@router.message(Command("sendfile"))
async def handle_sendfile_command(message: Message, command: CommandObject) -> None:
    """Upload a file from the host disk into the current chat/topic.

    The bot process performs the upload itself, so this works even when the
    engine session has no `bot` MCP server — e.g. managed CLI builds whose
    signed policy blocks extra MCP servers (see `claude_mcp_config_flags`).
    An agent that built an artifact only needs to report its absolute path.
    """
    args = (command.args or "").strip()
    if not args:
        await message.answer(t("ui.sendfile_usage"), parse_mode="HTML")
        return
    raw_path, caption = _sendfile_split_args(args)
    error = _sendfile_error(raw_path)
    if error is not None:
        key, fmt = error
        await message.answer(t(key, **fmt))
        return
    try:
        await message.answer_document(
            FSInputFile(Path(raw_path).expanduser()),
            caption=caption or None,
        )
    except Exception as exc:
        logger.warning("sendfile failed for %s", raw_path, exc_info=True)
        await message.answer(t("ui.sendfile_failed", error=str(exc)[:200]))


async def _restart_session_for_new_cwd(
    key: ChannelKey,
    *,
    exec_mode: str,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
) -> bool:
    """Drop the old session and (tmux mode) immediately respawn in the new cwd.

    A cwd change is only meaningful for a fresh session: a live TUI keeps
    its spawn-time directory regardless of the config override (observed
    2026-10-26: /cwd saved the override, then "New chat" respawned
    clear_context with the stale state.cwd), so the override write and the
    kill+respawn are one atomic operation. Returns False when the tmux
    respawn failed — the override stays saved and the next message
    lazy-starts the session.
    """
    if exec_mode == "tmux":
        await tmux_manager.kill(key)
        forward_batcher.clear(key)
        await message_queue.clear(key)
        await session_manager.kill_session(key)
        # _get_session re-resolves topic_config on every lookup, so the
        # session picks up the just-written cwd override here.
        session = session_manager._get_session(key)
        try:
            await tmux_manager.start_session(
                key,
                mode=session.mode,
                cwd=session.cwd,
                mcp_config=session.mcp_config,
                chat_id=session.chat_id,
                session_manager=session_manager,
                provider=session.engine,
                model=session.model,
            )
        except RuntimeError:
            logger.warning("tmux respawn after cwd change failed for %s", key, exc_info=True)
            return False
        return True
    # Subprocess mode: nothing long-lived to restart, but the persisted
    # subprocess session id belongs to the old cwd's transcript store —
    # resuming it under the new cwd would fail, so drop it.
    await session_manager.kill_session(key)
    return True


async def _apply_cwd_override(
    *,
    chat_id: int,
    thread_id: int,
    new_cwd: str | None,
    bot: Bot,
    settings: Settings,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
) -> str:
    """Persist a cwd override (None = bot default), restart the session there.

    Shared by `/cwd <path>`, `/cwd default` and the folder-picker callbacks
    so all three behave identically: config write → session kill+respawn →
    topic auto-rename. Returns the confirmation text (HTML).
    """
    ok = await topic_config.update_cwd(thread_id, new_cwd)
    if not ok:
        return t("ui.cwd_write_failed")

    key = (chat_id, thread_id)
    topic = topic_config.get_topic(thread_id)
    runtime = resolve_topic_runtime_config(topic, bot_defaults)
    restarted = await _restart_session_for_new_cwd(
        key,
        exec_mode=topic.exec_mode,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
        forward_batcher=forward_batcher,
    )
    if settings.topic_autoname:
        await sync_topic_name(
            bot,
            chat_id=chat_id,
            thread_id=thread_id,
            topic_config=topic_config,
            defaults=bot_defaults,
        )
    if not restarted:
        return t("ui.reset_failed")
    cwd = html.escape(str(runtime.cwd))
    if topic.exec_mode == "tmux":
        return t("ui.cwd_set_restarted", cwd=cwd)
    return t("ui.cwd_set_next", cwd=cwd)


def _cwd_picker_caption(cwd: Path, list_dir: Path, source: str) -> str:
    return t(
        "ui.cwd_picker_caption",
        cwd=html.escape(str(cwd)),
        source=source,
        parent=html.escape(str(list_dir)),
    )


async def _stale_cwd_picker(callback: CallbackQuery) -> None:
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
    await _answer_callback_safely(callback, t("ui.cwd_picker_stale"), show_alert=True)


def _cwd_callback_state(
    callback: CallbackQuery, cwd_picker_store: CwdPickerStore, token: str
) -> CwdPickerState | None:
    """Resolve picker state and verify the callback fired in the same topic."""
    state = cwd_picker_store.get(token)
    if state is None or callback.message is None:
        return None
    if isinstance(callback.message, InaccessibleMessage):
        return None
    if (callback.message.chat.id, callback.message.message_thread_id) != (
        state.chat_id,
        state.thread_id,
    ):
        return None
    return state


@router.callback_query(F.data.startswith("cw:p:"))
async def on_cwd_page(
    callback: CallbackQuery,
    cwd_picker_store: CwdPickerStore,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
) -> None:
    """Flip the folder-picker page; only the keyboard changes."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, token, raw_page = callback.data.split(":")
    state = _cwd_callback_state(callback, cwd_picker_store, token)
    if state is None:
        await _stale_cwd_picker(callback)
        return
    try:
        page = int(raw_page)
    except ValueError:
        await _stale_cwd_picker(callback)
        return
    dirs = await asyncio.to_thread(list_subdirs, state.list_dir)
    if not dirs:
        await _stale_cwd_picker(callback)
        return
    runtime = resolve_topic_runtime_config(topic_config.get_topic(state.thread_id), bot_defaults)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=cwd_keyboard(
                dirs,
                page=page,
                list_dir=state.list_dir,
                current_cwd=runtime.cwd,
                token=token,
            ),
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.callback_query(F.data.startswith("cw:u:"))
async def on_cwd_up(
    callback: CallbackQuery,
    cwd_picker_store: CwdPickerStore,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
) -> None:
    """Re-list the picker one directory level up."""
    if callback.data is None or callback.message is None:
        await callback.answer()
        return
    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return
    _, _, token = callback.data.split(":")
    state = _cwd_callback_state(callback, cwd_picker_store, token)
    if state is None:
        await _stale_cwd_picker(callback)
        return
    updated = cwd_picker_store.move_up(token)
    if updated is None:
        await _stale_cwd_picker(callback)
        return
    if updated.list_dir == state.list_dir:
        await callback.answer()  # filesystem root — nowhere to go
        return
    dirs = await asyncio.to_thread(list_subdirs, updated.list_dir)
    runtime = resolve_topic_runtime_config(topic_config.get_topic(state.thread_id), bot_defaults)
    source = (
        t("ui.cwd_source_override")
        if topic_config.get_topic(state.thread_id).cwd
        else t("ui.cwd_source_default")
    )
    try:
        await callback.message.edit_text(
            _cwd_picker_caption(runtime.cwd, updated.list_dir, source),
            reply_markup=cwd_keyboard(
                dirs,
                page=0,
                list_dir=updated.list_dir,
                current_cwd=runtime.cwd,
                token=token,
            ),
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


async def _on_cwd_dir_chosen(
    callback: CallbackQuery,
    token: str,
    path: Path,
    *,
    bot: Bot,
    settings: Settings,
    cwd_picker_store: CwdPickerStore,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
) -> None:
    """Apply a picker-chosen directory exactly like `/cwd <path>`."""
    state = _cwd_callback_state(callback, cwd_picker_store, token)
    if state is None:
        await _stale_cwd_picker(callback)
        return
    if not path.is_dir():
        await _answer_callback_safely(callback, t("ui.cwd_not_found", path=str(path)))
        return
    cwd_picker_store.drop(token)
    logger.info(
        "cwd picked via keyboard: user_id=%s thread_id=%s cwd=%s",
        callback.from_user.id if callback.from_user else None,
        state.thread_id,
        path,
    )
    text = await _apply_cwd_override(
        chat_id=state.chat_id,
        thread_id=state.thread_id,
        new_cwd=str(path),
        bot=bot,
        settings=settings,
        topic_config=topic_config,
        bot_defaults=bot_defaults,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
        forward_batcher=forward_batcher,
    )
    await _close_picker(callback)
    await callback.answer()
    if callback.message is not None and not isinstance(callback.message, InaccessibleMessage):
        await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data.startswith("cw:s:"))
async def on_cwd_pick(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    cwd_picker_store: CwdPickerStore,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
) -> None:
    """Apply one of the listed subdirectories as the new topic cwd."""
    if callback.data is None:
        await callback.answer()
        return
    _, _, token, raw_idx = callback.data.split(":")
    state = _cwd_callback_state(callback, cwd_picker_store, token)
    if state is None:
        await _stale_cwd_picker(callback)
        return
    try:
        idx = int(raw_idx)
    except ValueError:
        await _stale_cwd_picker(callback)
        return
    dirs = await asyncio.to_thread(list_subdirs, state.list_dir)
    if idx < 0 or idx >= len(dirs):
        await _stale_cwd_picker(callback)
        return
    await _on_cwd_dir_chosen(
        callback,
        token,
        dirs[idx],
        bot=bot,
        settings=settings,
        cwd_picker_store=cwd_picker_store,
        topic_config=topic_config,
        bot_defaults=bot_defaults,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
        forward_batcher=forward_batcher,
    )


@router.callback_query(F.data.startswith("cw:ok:"))
async def on_cwd_select_listed(
    callback: CallbackQuery,
    bot: Bot,
    settings: Settings,
    cwd_picker_store: CwdPickerStore,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
) -> None:
    """Select the directory the picker is currently listing as the new cwd."""
    if callback.data is None:
        await callback.answer()
        return
    _, _, token = callback.data.split(":")
    state = _cwd_callback_state(callback, cwd_picker_store, token)
    if state is None:
        await _stale_cwd_picker(callback)
        return
    await _on_cwd_dir_chosen(
        callback,
        token,
        state.list_dir,
        bot=bot,
        settings=settings,
        cwd_picker_store=cwd_picker_store,
        topic_config=topic_config,
        bot_defaults=bot_defaults,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
        forward_batcher=forward_batcher,
    )


@router.callback_query(F.data.startswith("cw:x:"))
async def on_cwd_cancel(callback: CallbackQuery, cwd_picker_store: CwdPickerStore) -> None:
    if callback.data is not None:
        cwd_picker_store.drop(callback.data.rsplit(":", 1)[-1])
    await _close_picker(callback)
    await _answer_callback_safely(callback, t("ui.cwd_picker_cancelled"))


@router.message(Command("cwd", "set_pwd"))
async def handle_cwd_command(
    message: Message,
    command: CommandObject,
    bot: Bot,
    settings: Settings,
    topic_config: TopicConfig,
    bot_defaults: BotDefaults,
    session_manager: SessionManager,
    tmux_manager: TmuxManager,
    message_queue: MessageQueue,
    forward_batcher: ForwardBatcher,
    cwd_picker_store: CwdPickerStore,
) -> None:
    """Show, browse, or change the working directory of the current forum topic.

    Bare `/cwd` posts a folder picker: subdirectories of the effective
    cwd's parent, one tap per project, with pagination and a "⬆ .." button
    to climb the tree. `/cwd <absolute path>` sets any directory directly,
    `/cwd default` clears the override. Every change kills the live session
    and respawns it in the new directory — the override never lags behind
    the running TUI.
    """
    _, thread_id = channel_key(message)
    if thread_id is None:
        await message.answer(t("ui.cwd_not_in_forum"))
        return

    topic = topic_config.get_topic(thread_id)
    runtime = resolve_topic_runtime_config(topic, bot_defaults)
    source = t("ui.cwd_source_override") if topic.cwd else t("ui.cwd_source_default")
    arg = (command.args or "").strip()

    if not arg:
        parent = runtime.cwd.parent
        dirs = await asyncio.to_thread(list_subdirs, parent)
        if not dirs:
            await message.answer(
                t("ui.cwd_current", cwd=html.escape(str(runtime.cwd)), source=source)
                + "\n"
                + t("ui.cwd_picker_empty", parent=html.escape(str(parent))),
                parse_mode="HTML",
            )
            return
        token = cwd_picker_store.put(
            CwdPickerState(
                chat_id=message.chat.id,
                thread_id=thread_id,
                list_dir=parent,
                created_at=time.time(),
            )
        )
        await message.answer(
            _cwd_picker_caption(runtime.cwd, parent, source),
            reply_markup=cwd_keyboard(
                dirs,
                page=0,
                list_dir=parent,
                current_cwd=runtime.cwd,
                token=token,
            ),
            parse_mode="HTML",
        )
        return

    if arg.lower() == "default":
        logger.info("cwd override cleared: thread_id=%s", thread_id)
        text = await _apply_cwd_override(
            chat_id=message.chat.id,
            thread_id=thread_id,
            new_cwd=None,
            bot=bot,
            settings=settings,
            topic_config=topic_config,
            bot_defaults=bot_defaults,
            session_manager=session_manager,
            tmux_manager=tmux_manager,
            message_queue=message_queue,
            forward_batcher=forward_batcher,
        )
        await message.answer(text, parse_mode="HTML")
        return

    path = Path(arg).expanduser()
    if not path.is_absolute():
        await message.answer(t("ui.cwd_not_absolute"))
        return
    if not path.is_dir():
        await message.answer(t("ui.cwd_not_found", path=html.escape(arg)))
        return

    logger.info(
        "cwd override set: user_id=%s thread_id=%s cwd=%s",
        message.from_user and message.from_user.id,
        thread_id,
        path,
    )
    text = await _apply_cwd_override(
        chat_id=message.chat.id,
        thread_id=thread_id,
        new_cwd=str(path),
        bot=bot,
        settings=settings,
        topic_config=topic_config,
        bot_defaults=bot_defaults,
        session_manager=session_manager,
        tmux_manager=tmux_manager,
        message_queue=message_queue,
        forward_batcher=forward_batcher,
    )
    await message.answer(text, parse_mode="HTML")
