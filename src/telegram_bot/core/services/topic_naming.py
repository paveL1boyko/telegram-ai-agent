"""Auto-naming of forum topics from the topic's cwd and model override.

When `Settings.topic_autoname` is on, `/cwd` changes and model/engine
switches rename the Telegram topic to `<cwd-folder> · <short-model>` so the
topic list reads like a dashboard of running sessions. The rename goes
through the Bot API `editForumTopic`; the resulting `forum_topic_edited`
update flows back into `handlers/forum_topic.py`, which syncs the name into
topic_config.json — the config stays a mirror of what Telegram shows.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config

logger = logging.getLogger(__name__)

# Telegram caps forum topic names at 128 chars.
_TOPIC_NAME_MAX = 128

# Claude-family ids collapse to the family name in the topic title:
# claude-fable-5 → fable, claude-opus-5-5 → opus. Other vendors keep their
# full tail (kimi-k3, gpt-5.5) — there is no shorter canonical form.
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


def topic_display_name(cwd: str | Path, model: str | None) -> str:
    """`<cwd folder>` or `<cwd folder> · <short model>` — the auto-name."""
    base = Path(cwd).name or str(cwd)
    short = short_model_name(model)
    if short:
        base = f"{base} · {short}"
    return base[:_TOPIC_NAME_MAX]


async def sync_topic_name(
    bot: Bot,
    *,
    chat_id: int,
    thread_id: int,
    topic_config: TopicConfig,
    defaults: BotDefaults,
) -> None:
    """Rename the forum topic to match its current cwd + model.

    Best-effort: any Telegram failure (missing manage_topics right, topic
    deleted, name unchanged) is logged and swallowed — a failed rename must
    never block the cwd/model change it shadows.
    """
    topic = topic_config.get_topic(thread_id)
    if topic.type == "assistant":
        # Assistant topics have no cwd concept; auto-naming would collapse
        # them all to the bot default folder name.
        return
    runtime = resolve_topic_runtime_config(topic, defaults)
    name = topic_display_name(runtime.cwd, runtime.model)
    if name == topic.name:
        return
    try:
        await bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=name)
    except TelegramBadRequest:
        logger.warning(
            "topic autoname failed thread_id=%s name=%r (manage_topics right?)",
            thread_id,
            name,
            exc_info=True,
        )
    logger.info("topic autoname: thread_id=%s → %r", thread_id, name)
