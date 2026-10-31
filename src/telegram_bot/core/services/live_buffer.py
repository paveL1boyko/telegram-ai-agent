"""LiveStatusBuffer — one editable Telegram message for streamed CC progress.

Agent-team runs emit many tool_use / task_progress / task_completed events
per user message. Shipping each as its own SendMessage rapidly hits the
20 msg/min group flood limit and freezes the tail (see tmux_manager for
the async queue mitigation). LiveStatusBuffer collapses the same signal
into a single message that is edited in place with a throttle, and
rotates to a fresh message when the 4096-char limit looms.

Lifecycle:

    buf = LiveStatusBuffer(bot=..., chat_id=..., thread_id=...)
    await buf.append("🔧 Read: foo.py")
    await buf.append("🤖 researcher → scanning")
    ...
    await buf.close()   # idempotent, flushes, cancels worker

Rendering: all lines but the last are wrapped in an expandable
blockquote (collapses to a one-line preview in the chat); the latest
line stays outside the quote so current activity is always visible.

The first page is posted lazily, with the first flush that has lines to
show: a turn without tool calls then leaves no progress message at all.
Pass `initial_message_id` to adopt an already-sent message as the first
page instead.

Threading model:

- A background asyncio.Task (`_worker`) wakes up every `throttle_sec` and
  edits the current message if there are unflushed lines.
- `append` only mutates the in-memory buffer; it never awaits Telegram.
  This is deliberate: callers live inside tmux_manager._sender_loop, and
  we don't want a flood wait on edit to back-pressure the sender.
- Flood waits inside `_worker` are logged and slept; on the next tick
  we retry with whatever is pending.
- `close` sets a closed flag, cancels the worker, and awaits it. Safe to
  call repeatedly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

logger = logging.getLogger(__name__)

_MAX_CHARS = 3800  # leave headroom under Telegram's 4096-char cap
_DEFAULT_THROTTLE_SEC = 5.0  # min gap between edits of the same message
_ROTATE_MIN_INTERVAL_SEC = 4.0  # avoid hitting SendMessage flood via rotations
_HEADER_CONTINUED = "⏳ Думаю…\n"


def _ts() -> str:
    """Local-time HH:MM:SS prefix. The buffer renders for the chat UI, so
    it speaks the host's timezone (the operator's own in a self-hosted
    deployment) — a UTC prefix reads as "3 hours off" for anyone not
    actually in UTC."""
    return datetime.now().astimezone().strftime("%H:%M:%S")


class LiveStatusBuffer:
    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        thread_id: int | None,
        initial_message_id: int | None = None,
        throttle_sec: float = _DEFAULT_THROTTLE_SEC,
        max_chars: int = _MAX_CHARS,
        rotate_min_interval_sec: float = _ROTATE_MIN_INTERVAL_SEC,
        header_text: str = "",
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._current_message_id: int | None = initial_message_id
        self._throttle_sec = throttle_sec
        self._max_chars = max_chars
        self._rotate_min_interval = rotate_min_interval_sec
        self._header_text = header_text

        self._lines: list[str] = []  # rendered lines for the current page
        self._is_continuation = False  # header toggles on after the first rotate
        self._dirty = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._last_rotate: float = time.monotonic()
        self._last_edit_text: str = ""  # skip no-op edits
        # all pages we've posted
        self._message_ids: list[int] = [] if initial_message_id is None else [initial_message_id]

        self._worker: asyncio.Task[None] | None = asyncio.create_task(self._worker_loop())

    # --- Public API ---

    @property
    def message_ids(self) -> list[int]:
        """All pages posted by this buffer (for reply-to-resume recording)."""
        return list(self._message_ids)

    @property
    def has_lines(self) -> bool:
        """True once at least one status line has been appended."""
        return bool(self._lines)

    async def finalize(self, text: str) -> bool:
        """Turn an untouched placeholder into the turn-completion marker.

        A pure-text turn (no tool calls) never appends a line, so an adopted
        placeholder would otherwise hang as "Thinking..." above the finished
        answer — reading as a stuck stream. Returns True iff the buffer had
        no lines and its message was edited to `text`; the caller then skips
        sending the marker as a separate message. A buffer with status
        history keeps its page untouched and gets the marker separately, as
        does a lazy buffer that never posted a page.
        """
        if self._closed or self._lines or self._current_message_id is None:
            return False
        await self.close()  # stops the worker first, so it can't re-edit after us
        await self._edit(self._current_message_id, text)
        return True

    async def append(self, line: str) -> None:
        """Record a status line. Never awaits Telegram — the worker does that."""
        if self._closed or not line:
            return
        # Long single events (huge Bash outputs embedded in a status) would
        # exceed max_chars in one go; truncate with an ellipsis so the buffer
        # stays edit-friendly. Reserve headroom for the timestamp prefix
        # ("[HH:MM:SS] ", ~11 chars) and the possible continuation header so
        # the rendered line alone never blows past max_chars.
        max_raw = max(32, self._max_chars - 32)
        if len(line) > max_raw:
            line = line[: max_raw - 1] + "…"
        async with self._lock:
            rendered = f"[{_ts()}] {line}"
            self._lines.append(rendered)
            self._dirty = True

    async def close(self) -> None:
        """Flush pending lines and cancel worker. Idempotent."""
        if self._closed:
            return
        self._closed = True

        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        # Final flush under the lock — pending lines must end up somewhere.
        # Failures are logged, not raised.
        try:
            await self._flush_if_dirty(final=True)
        except Exception:
            logger.warning("LiveStatusBuffer final flush failed", exc_info=True)

    # --- Worker loop ---

    async def _worker_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._throttle_sec)
                try:
                    await self._flush_if_dirty(final=False)
                except Exception:
                    logger.warning("LiveStatusBuffer worker edit failed", exc_info=True)
        except asyncio.CancelledError:
            raise

    async def _flush_if_dirty(self, *, final: bool) -> None:
        """Edit the current page with the latest content, rotating when full."""
        async with self._lock:
            if not self._dirty and not final:
                return
            if self._current_message_id is None and not self._lines:
                return  # lazy first page: nothing to show yet
            text = self._render()
            self._dirty = False

        if text == self._last_edit_text:
            return

        if self._current_message_id is None:
            if len(text) > self._max_chars:
                async with self._lock:
                    while len(self._lines) > 1 and len(self._render()) > self._max_chars:
                        self._lines.pop(0)
                    text = self._render()
            new_id = await self._send_new_page(text)
            if new_id is None:
                async with self._lock:
                    self._dirty = True  # retry on the next tick
                return
            self._current_message_id = new_id
            self._message_ids.append(new_id)
            self._last_edit_text = text
            return

        if len(text) > self._max_chars:
            # Rotate before the edit so we never send >4096 to Telegram. Under
            # lock: _rotate may mutate self._lines (drop what fit on the old
            # page, keep the rest for the new one).
            async with self._lock:
                rotated = await self._rotate_locked()
            if rotated:
                # Re-render for the new page after rotation
                async with self._lock:
                    text = self._render()
                    self._dirty = False
            else:
                # Rotation suppressed by min-interval — trim oldest lines so
                # the edit still fits. Favor the fresh signal over history.
                async with self._lock:
                    while self._lines and len(self._render()) > self._max_chars:
                        self._lines.pop(0)
                    text = self._render()

        await self._edit(self._current_message_id, text)
        self._last_edit_text = text

    def _render(self) -> str:
        # header_text and _is_continuation are mutually exclusive: the first page
        # shows header_text; continuation pages show _HEADER_CONTINUED instead.
        # Both branches include the header in the rendered text, so the overflow
        # guard (len(render()) > max_chars) and the trim loop already account for
        # the header length — body capacity is implicitly max_chars - len(header).
        body = self._render_body()
        if self._is_continuation:
            return _HEADER_CONTINUED + body
        elif self._header_text:
            return self._header_text + "\n" + body
        return body

    def _render_body(self) -> str:
        """History collapses into an expandable blockquote; the latest line
        stays outside the quote so current activity is visible in the chat
        without tapping "show more". Telegram has no font-size entity, so
        collapsing is the only way to shrink the log's visual footprint.
        The collapsed preview shows the quote's first (oldest) line — the
        chronological anchor of the page."""
        if len(self._lines) <= 1:
            return "\n".join(self._lines)
        quoted = "\n".join(self._lines[:-1])
        return f"<blockquote expandable>{quoted}</blockquote>\n{self._lines[-1]}"

    async def _rotate_locked(self) -> bool:
        """Close the current page and start a new one. Must be called under lock.

        Returns False if rotation is skipped due to min-interval throttling.
        """
        now = time.monotonic()
        if now - self._last_rotate < self._rotate_min_interval:
            return False
        self._last_rotate = now

        # Send a fresh placeholder for the continuation page.
        new_id = await self._send_new_page(_HEADER_CONTINUED.strip())
        if new_id is None:
            # If we can't post the new page, keep trimming on the old one; the
            # alternative (giving up) leaves the user with a frozen buffer.
            self._last_rotate = now - self._rotate_min_interval
            return False

        self._current_message_id = new_id
        self._message_ids.append(new_id)
        self._is_continuation = True
        self._last_edit_text = ""
        # Empty the buffer for the new page — carry-over would bloat it
        # immediately and likely trigger another rotation.
        self._lines = []
        return True

    # --- Telegram plumbing ---

    async def _edit(self, message_id: int, text: str) -> None:
        try:
            await self._bot.edit_message_text(
                text=text,
                chat_id=self._chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
            )
        except TelegramRetryAfter as e:
            logger.warning(
                "LiveStatusBuffer flood wait on edit (chat %s, msg %s): retry_after=%ds",
                self._chat_id,
                message_id,
                e.retry_after,
            )
            await asyncio.sleep(e.retry_after)
            # No retry here — worker wakes on its next throttle and tries again.
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.warning(
                "LiveStatusBuffer edit failed (chat %s, msg %s): %s",
                self._chat_id,
                message_id,
                e,
            )

    async def _send_new_page(self, text: str) -> int | None:
        try:
            sent = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text or "…",
                message_thread_id=self._thread_id,
                disable_notification=True,
                parse_mode=ParseMode.HTML,
            )
            return sent.message_id
        except TelegramRetryAfter as e:
            logger.warning(
                "LiveStatusBuffer flood wait on rotate send (chat %s): retry_after=%ds",
                self._chat_id,
                e.retry_after,
            )
            await asyncio.sleep(e.retry_after)
            return None
        except Exception:
            logger.warning("LiveStatusBuffer failed to send new page", exc_info=True)
            return None
