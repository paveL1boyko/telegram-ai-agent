"""Auto-naming of forum topics from the topic's cwd and model override.

When `Settings.topic_autoname` is on, `/cwd` changes and model/engine
switches rename the Telegram topic to `<cwd-abbrev> · <short-model>` so the
topic list reads like a dashboard of running sessions. The cwd folder is
abbreviated to the first two letters of each word (`telegram-ai-agent` →
`Te.Ai.Ag`) — full folder names made the topic list unreadable on a phone.
The rename goes through the Bot API `editForumTopic`; the resulting
`forum_topic_edited` update flows back into `handlers/forum_topic.py`,
which syncs the name into topic_config.json — the config stays a mirror of
what Telegram shows.

Topics named under an older scheme catch up without a cwd/model change:
`sync_all_topic_names` runs once at bot startup over every channel with a
persisted tmux session, and `sync_topic_name_once` runs on the first user
message in a topic — so topics without a tmux session are renamed as soon
as they are used.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

# Telegram caps forum topic names at 128 chars.
_TOPIC_NAME_MAX = 128

# Word boundaries inside a folder name: separator runs (`-`, `_`, `.`, space)
# and lower→Upper camelCase transitions (ASCII only — Cyrillic folders use
# separators). Digits stay attached to their word.
_WORD_SEP_RE = re.compile(r"[\W_]+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Letters kept from each word of the folder name.
_ABBREV_WORD_LEN = 2

# Claude-family ids collapse to the family name in the topic title:
# claude-fable-5 → fable, claude-opus-5-5 → opus. Other vendors keep their
# full tail (kimi-k3, gpt-5.5) — the user prefers versions visible.
_CLAUDE_FAMILY_RE = re.compile(r"(?i)^(opus|sonnet|haiku|fable)(?:-\d[\d.-]*)?$")

# Trailing context-window flag — `anthropic/claude-fable-5[1m]` → `claude-fable-5`.
_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]$")


def short_model_name(model: str | None) -> str | None:
    """Compress a model override to a short suffix for the topic name.

    `anthropic/claude-fable-5[1m]` → `fable`, `openrouter/openai/gpt-5.5[1m]`
    → `gpt-5.5`, `openrouter/moonshotai/kimi-k3[1m]` → `kimi-k3`,
    `None` → `None` (no suffix).
    """
    if not model:
        return None
    tail = model.rsplit("/", 1)[-1]
    tail = _CONTEXT_SUFFIX_RE.sub("", tail)
    tail = tail.removeprefix("claude-")
    family = _CLAUDE_FAMILY_RE.fullmatch(tail)
    if family:
        return family.group(1).lower()
    return tail or None


def abbreviate_folder(name: str) -> str:
    """Compress a folder name to the first letters of each word.

    `telegram-ai-agent` → `Te.Ai.Ag`, `ecom_merchants_partners` → `Ec.Me.Pa`,
    `xpathGenerator` → `Xp.Ge`, `ceneo` → `Ce`. Each piece is capitalized so
    the dots read as word boundaries rather than a file extension. A name
    with no letters or digits at all is returned unchanged.
    """
    words = [w for w in _WORD_SEP_RE.split(_CAMEL_RE.sub(" ", name)) if w]
    if not words:
        return name
    return ".".join(w[:_ABBREV_WORD_LEN].capitalize() for w in words)


def topic_display_name(cwd: str | Path, model: str | None, emoji: str | None = None) -> str:
    """`[<emoji> ]<cwd abbrev>[ · <short model>]` — the auto-name.

    `emoji` is only inlined when it cannot be the topic icon (see
    `topic_icon_id`); callers pass None when the icon carries it.
    """
    folder = Path(cwd).name
    base = abbreviate_folder(folder) if folder else str(cwd)
    short = short_model_name(model)
    if short:
        base = f"{base} · {short}"
    if emoji:
        base = f"{emoji} {base}"
    return base[:_TOPIC_NAME_MAX]


# emoji → custom_emoji_id of Telegram's forum topic icon set. Populated once
# per process by `load_topic_icons`; empty means "unknown" and every emoji
# falls back to a name prefix. Keys are stored without U+FE0F so a
# hand-typed `⚡` matches the set's `⚡️`.
_topic_icons: dict[str, str] = {}
_icon_emoji: dict[str, str] = {}

_VARIATION_SELECTOR = "️"

# Fallback palette for `default_topic_emoji` when the icon set could not be
# fetched — all members of that set, so a later fetch upgrades them to real
# icons without changing the config.
_FALLBACK_EMOJI = ("🤖", "💻", "🧪", "🔬", "🔭", "💡", "📁", "🔎", "🧠", "🎨", "💎", "🏁")


def _icon_key(emoji: str) -> str:
    return emoji.replace(_VARIATION_SELECTOR, "")


def set_topic_icons(icons: Mapping[str, str]) -> None:
    """Replace the cached forum icon catalog (tests, or a manual refresh)."""
    _topic_icons.clear()
    _icon_emoji.clear()
    for emoji, icon_id in icons.items():
        _topic_icons[_icon_key(emoji)] = icon_id
        _icon_emoji[icon_id] = emoji


async def load_topic_icons(bot: Bot) -> dict[str, str]:
    """Fetch the forum topic icon set once; best-effort, empty on failure."""
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
    except Exception:
        logger.warning("forum topic icon set unavailable; emoji stay in topic names", exc_info=True)
        return dict(_topic_icons)
    icons = {s.emoji: s.custom_emoji_id for s in stickers if s.emoji and s.custom_emoji_id}
    set_topic_icons(icons)
    logger.info("forum topic icon set loaded: %d emoji", len(icons))
    return icons


def topic_icon_id(emoji: str | None) -> str | None:
    """custom_emoji_id for `emoji` if Telegram allows it as a topic icon."""
    if not emoji:
        return None
    return _topic_icons.get(_icon_key(emoji))


def topic_icon_emoji(icon_custom_emoji_id: str | None) -> str | None:
    """Reverse lookup: the emoji behind a topic icon id, None if unknown."""
    if not icon_custom_emoji_id:
        return None
    return _icon_emoji.get(icon_custom_emoji_id)


def default_topic_emoji(thread_id: int, taken: Iterable[str]) -> str:
    """Pick a marker for a topic that has none, distinct from `taken` when possible.

    Deterministic in `thread_id` so a config rewrite without the field
    lands on the same emoji again; drawn from the fallback palette, which is
    a subset of the forum icon set, so the pick is always a real icon.
    """
    used = {_icon_key(e) for e in taken}
    free = [e for e in _FALLBACK_EMOJI if _icon_key(e) not in used] or list(_FALLBACK_EMOJI)
    return free[thread_id % len(free)]


def _resolve_topic_title(
    thread_id: int, topic_config: TopicConfig, defaults: BotDefaults
) -> tuple[str, str | None] | None:
    """(name, icon_custom_emoji_id) for a project topic; None for assistant topics."""
    topic = topic_config.get_topic(thread_id)
    if topic.type == "assistant":
        # Assistant topics have no cwd concept; auto-naming would collapse
        # them all to the bot default folder name.
        return None
    runtime = resolve_topic_runtime_config(topic, defaults)
    icon_id = topic_icon_id(topic.emoji)
    name = topic_display_name(runtime.cwd, runtime.model, None if icon_id else topic.emoji)
    return name, icon_id


async def _edit_topic(
    bot: Bot, *, chat_id: int, thread_id: int, name: str, icon_id: str | None
) -> bool:
    """editForumTopic wrapper: True on success, False (logged) on Telegram refusal."""
    try:
        await bot.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=thread_id,
            name=name,
            icon_custom_emoji_id=icon_id,
        )
    except TelegramBadRequest as exc:
        if "TOPIC_NOT_MODIFIED" in exc.message:
            return True
        logger.warning(
            "topic autoname failed thread_id=%s name=%r (manage_topics right?)",
            thread_id,
            name,
            exc_info=True,
        )
        return False
    logger.info("topic autoname: thread_id=%s → %r icon=%s", thread_id, name, icon_id)
    return True


async def sync_topic_name(
    bot: Bot,
    *,
    chat_id: int,
    thread_id: int,
    topic_config: TopicConfig,
    defaults: BotDefaults,
    force: bool = False,
) -> None:
    """Rename the forum topic to match its current cwd + model + emoji.

    Best-effort: any Telegram failure (missing manage_topics right, topic
    deleted, name unchanged) is logged and swallowed — a failed rename must
    never block the cwd/model change it shadows. `force` skips the
    name-unchanged short-circuit so the icon is (re)applied too — the config
    mirrors the name but not the icon, so icon drift is invisible otherwise.
    """
    resolved = _resolve_topic_title(thread_id, topic_config, defaults)
    if resolved is None:
        return
    name, icon_id = resolved
    if not force and name == topic_config.get_topic(thread_id).name:
        return
    await _edit_topic(bot, chat_id=chat_id, thread_id=thread_id, name=name, icon_id=icon_id)


# Channels whose name was already reconciled in this process — the lazy
# first-message sync must not hit Telegram on every message, nor keep
# retrying a topic the bot has no manage_topics right in.
_synced_channels: set[ChannelKey] = set()


async def ensure_topic_emoji(thread_id: int, topic_config: TopicConfig) -> None:
    """Assign and persist a default emoji for a freshly created project topic.

    Writes the marker into topic_config.json so the operator sees an `emoji`
    line they can edit by hand. Only for new topics: existing ones may carry
    an icon set through the Telegram UI that the config does not know about,
    and a generated default would overwrite it.
    """
    topic = topic_config.get_topic(thread_id)
    if topic.type == "assistant" or topic.emoji:
        return
    taken = [t.emoji for t in topic_config.all_topics().values() if t.emoji]
    await topic_config.update_emoji(thread_id, default_topic_emoji(thread_id, taken))


async def sync_topic_name_once(
    bot: Bot,
    *,
    channel_key: ChannelKey,
    topic_config: TopicConfig,
    defaults: BotDefaults,
) -> None:
    """First-message catch-up: apply the current naming scheme to a topic once per process.

    Covers topics that were named under an older scheme and have no tmux
    session for the startup pass to find. Subsequent renames come from
    `/cwd`, engine/model switches and config edits via `sync_topic_name`.
    """
    if channel_key in _synced_channels or channel_key[1] is None:
        return
    _synced_channels.add(channel_key)
    await sync_topic_name(
        bot,
        chat_id=channel_key[0],
        thread_id=channel_key[1],
        topic_config=topic_config,
        defaults=defaults,
        force=True,
    )


async def sync_all_topic_names(
    bot: Bot,
    *,
    channel_keys: Iterable[ChannelKey],
    topic_config: TopicConfig,
    defaults: BotDefaults,
) -> int:
    """Startup pass: rename (and re-badge, where `emoji` is set) every known project topic.

    Returns the number of topics whose edit succeeded. Telegram rate-limits
    editForumTopic like any send, so the loop is sequential and tolerates
    per-topic failures. A topic without `emoji` keeps whatever icon it has.
    """
    renamed = 0
    for chat_id, thread_id in sorted({(c, t) for c, t in channel_keys if t is not None}):
        resolved = _resolve_topic_title(thread_id, topic_config, defaults)
        if resolved is None:
            continue
        name, icon_id = resolved
        _synced_channels.add((chat_id, thread_id))
        if await _edit_topic(bot, chat_id=chat_id, thread_id=thread_id, name=name, icon_id=icon_id):
            renamed += 1
    return renamed
