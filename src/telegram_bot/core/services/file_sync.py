"""File mirror — keeps Telegram copies of watched on-disk folders in sync.

A topic configured via `/filesdir` (its `files_dirs` field in
topic_config.json) mirrors a set of local folders: every file inside is
uploaded to that topic once, re-uploaded when it changes, and its Telegram
message deleted when the file disappears from disk. Documents cannot be
edited through the Bot API, so "updated" always means: send the new copy,
then delete the old message — never the reverse, so a failed send loses
nothing.

Per-topic state lives in `<state_dir>/<thread_id>.json` and maps each file
to its message id plus the last seen (mtime, size). The chat id is learned
from the first `/filesdir`/`/files` run inside the topic and persisted
there too — the watchdog itself only knows thread ids from the config, so
a mirror set up by hand-editing topic_config.json posts nothing until the
user runs `/files` in that topic once.

Stability rule: a file uploads only after two consecutive ticks see the
same (mtime, size), so an agent still writing the file never ships a torn
read. `/files` bypasses the wait with force=True.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from telegram_bot.core.messages import t
from telegram_bot.core.services.topic_config import TopicConfig

logger = logging.getLogger(__name__)

# Telegram Bot API upload limit for the sendDocument method.
MAX_SYNC_FILE_BYTES = 50 * 1024 * 1024

# Pruned while walking the mirror folders: VCS/build noise never belongs in
# a Telegram mirror. Hidden entries (dot-prefixed) are skipped as a class.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".ruff_cache"}


@dataclass
class SyncResult:
    """Counts of one sync run — user-facing summary and tests."""

    posted: int = 0
    updated: int = 0
    removed: int = 0


def scan_mirror_files(entries: list[dict[str, str]]) -> dict[str, tuple[int, int]]:
    """Snapshot of every mirrored file: {abspath: (mtime_ns, size)}.

    Each entry is {"dir": ..., "pattern": pathlib glob relative to dir} —
    "*.md" mirrors a flat folder, "**/*.pdf" a tree. Files over the Bot API
    upload limit, hidden files and skip-listed directories are pruned.
    """
    snapshot: dict[str, tuple[int, int]] = {}
    for entry in entries:
        base = Path(entry["dir"])
        pattern = entry.get("pattern") or "**/*"
        for path in base.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(base)
            if any(part.startswith(".") or part in _SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if path.name.startswith("."):
                continue
            try:
                st = path.stat()
            except OSError:
                # Removed between the listing and the stat — next tick
                # will simply not see it.
                continue
            if st.st_size > MAX_SYNC_FILE_BYTES:
                continue
            snapshot[str(path)] = (st.st_mtime_ns, st.st_size)
    return snapshot


def count_mirror_files(entries: list[dict[str, str]]) -> int:
    """Number of files a set of mirror entries would upload.

    Used by `/filesdir` to refuse oversized folders up front instead of
    letting the watchdog discover a flood one message at a time.
    """
    return len(scan_mirror_files(entries))


class FileSyncService:
    """Watchdog + sync core for per-topic folder mirrors."""

    def __init__(self, *, bot: Bot, topic_config: TopicConfig, state_dir: Path) -> None:
        self._bot = bot
        self._topic_config = topic_config
        self._state_dir = state_dir
        self._locks: dict[int, asyncio.Lock] = {}
        self._watch_task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, interval_sec: float) -> None:
        """Run the auto-sync loop in the background (idempotent)."""
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop(interval_sec))

    async def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task
            self._watch_task = None

    async def _watch_loop(self, interval_sec: float) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                await self.sync_all()
            except Exception:
                # One broken topic must not kill the loop for the rest.
                logger.warning("File sync watchdog tick failed", exc_info=True)

    async def sync_all(self) -> None:
        """Sync every configured mirror once (one watchdog tick)."""
        for thread_id, topic in self._topic_config.all_topics().items():
            if not topic.files_dirs:
                continue
            chat_id_raw = self._load_state(thread_id).get("chat_id")
            if not isinstance(chat_id_raw, int):
                # Never interacted with this topic — the chat id is not
                # knowable from the config alone.
                continue
            try:
                await self.sync_topic(chat_id_raw, thread_id, topic.files_dirs)
            except Exception:
                logger.warning("File sync failed for topic %d", thread_id, exc_info=True)

    # -- sync core ---------------------------------------------------------

    async def sync_topic(
        self,
        chat_id: int,
        thread_id: int,
        entries: list[dict[str, str]],
        *,
        force: bool = False,
    ) -> SyncResult:
        """Bring the Telegram mirror of `entries` up to date in one topic.

        force=True uploads immediately without the two-tick stability wait
        (manual `/files`, first `/filesdir`); the watchdog passes force=False.
        Uploads go in sorted path order, so zero-padded names
        (00-report.md, 01-plan.md …) land in the topic by number.
        """
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            return await self._sync_locked(chat_id, thread_id, entries, force=force)

    async def rebuild_topic(
        self, chat_id: int, thread_id: int, entries: list[dict[str, str]]
    ) -> SyncResult:
        """Delete every tracked message and re-upload all files in sorted order.

        Telegram cannot reorder existing messages, so an updated file always
        lands at the bottom of the topic. This is the manual "re-sort":
        clear the mirror and rebuild it — `/files resort`.
        """
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            state = self._load_state(thread_id)
            files_raw = state.get("files")
            if isinstance(files_raw, dict):
                for entry in files_raw.values():
                    if isinstance(entry, dict) and isinstance(entry.get("msg_id"), int):
                        await self._delete_message(chat_id, entry["msg_id"])
            state["files"] = {}
            state["pending"] = {}
            self._save_state(thread_id, state)
            return await self._sync_locked(chat_id, thread_id, entries, force=True)

    async def _sync_locked(
        self,
        chat_id: int,
        thread_id: int,
        entries: list[dict[str, str]],
        *,
        force: bool,
    ) -> SyncResult:
        """Sync core — the caller already holds the per-topic lock."""
        state = self._load_state(thread_id)
        files_raw = state.get("files")
        files: dict[str, dict[str, int]] = dict(files_raw) if isinstance(files_raw, dict) else {}
        pending_raw = state.get("pending")
        pending: dict[str, list[int]] = dict(pending_raw) if isinstance(pending_raw, dict) else {}
        state["files"] = files
        state["pending"] = pending
        state["chat_id"] = chat_id

        snapshot = scan_mirror_files(entries)
        result = SyncResult()

        for path, (mtime_ns, size) in sorted(snapshot.items()):
            entry = files.get(path)
            entry_sig = (entry["mtime_ns"], entry["size"]) if entry else None
            if entry_sig == (mtime_ns, size):
                pending.pop(path, None)
                continue
            if not force and pending.get(path) != [mtime_ns, size]:
                # First sighting of this content — wait one more tick
                # so a file mid-write never ships torn.
                pending[path] = [mtime_ns, size]
                continue
            pending.pop(path, None)
            if entry is None:
                msg_id = await self._send_file(chat_id, thread_id, path, updated=False)
                if msg_id is not None:
                    files[path] = {"mtime_ns": mtime_ns, "size": size, "msg_id": msg_id}
                    result.posted += 1
            else:
                # Send first, delete after — a failed send must not
                # lose the previous copy.
                msg_id = await self._send_file(chat_id, thread_id, path, updated=True)
                if msg_id is not None:
                    await self._delete_message(chat_id, entry["msg_id"])
                    files[path] = {"mtime_ns": mtime_ns, "size": size, "msg_id": msg_id}
                    result.updated += 1

        for path in [p for p in files if p not in snapshot]:
            await self._delete_message(chat_id, files[path]["msg_id"])
            del files[path]
            result.removed += 1
        for path in [p for p in pending if p not in snapshot]:
            del pending[path]

        self._save_state(thread_id, state)
        if result.posted or result.updated or result.removed:
            logger.info(
                "File sync topic %d: %d posted, %d updated, %d removed",
                thread_id,
                result.posted,
                result.updated,
                result.removed,
            )
        return result

    # -- Telegram plumbing -------------------------------------------------

    async def _send_file(
        self, chat_id: int, thread_id: int, path: str, *, updated: bool
    ) -> int | None:
        """Upload one file, returning its message id (None on failure)."""
        try:
            if updated:
                caption = t("ui.files_caption_updated", time=_clock_time())
            else:
                caption = t("ui.files_caption_new")
            msg = await self._bot.send_document(
                chat_id,
                document=FSInputFile(Path(path)),
                caption=caption,
                message_thread_id=thread_id,
            )
        except Exception as exc:
            logger.warning("File mirror: failed to send %s: %s", path, exc)
            return None
        return msg.message_id

    async def _delete_message(self, chat_id: int, msg_id: int) -> None:
        """Best-effort delete: >48h-old messages are not deletable by bots."""
        try:
            await self._bot.delete_message(chat_id, msg_id)
        except TelegramBadRequest:
            logger.info("File mirror: cannot delete old message %d (past the 48h window?)", msg_id)

    # -- state -------------------------------------------------------------

    def _state_path(self, thread_id: int) -> Path:
        return self._state_dir / f"{thread_id}.json"

    def _load_state(self, thread_id: int) -> dict[str, object]:
        """Read one topic's mirror state; empty state on missing/corrupt file.

        The watchdog reads from disk every tick instead of caching, so
        manual `/files` and watchdog writes can never diverge.
        """
        path = self._state_path(thread_id)
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            logger.warning("File mirror state for topic %d is corrupt, resetting", thread_id)
            return {}
        if not isinstance(state, dict):
            return {}
        return state

    def _save_state(self, thread_id: int, state: dict[str, object]) -> None:
        path = self._state_path(thread_id)
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("File mirror: cannot save state for topic %d: %s", thread_id, exc)


def _clock_time() -> str:
    """Local HH:MM for the "updated" caption."""
    return time.strftime("%H:%M")
