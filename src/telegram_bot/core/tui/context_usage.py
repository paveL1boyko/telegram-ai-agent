"""Context-window size of a live Claude Code session, read from its transcript.

Claude Code writes one JSONL line per API round-trip; every `assistant`
line carries the request's `usage` block. The context window the model
saw on that call is the sum of the three input counters (uncached, cache
write, cache read) — output tokens are not part of the window yet. The
newest assistant line therefore tells how big the conversation currently
is, which is what the auto-compact watchdog compares against its
threshold (see `TmuxManager._check_channel_context`).

Only the file tail is read: transcripts grow to tens of megabytes, and a
usage block is always within the last few hundred KB. Codex/Nessy
transcripts have no comparable field and report None.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# How much of the file tail to scan for the newest assistant line. A tool
# result can be several hundred KB (a big file read), so one line alone can
# push the previous assistant entry far back; 1 MB covers that with margin
# while staying cheap to read every few minutes.
_TAIL_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ContextUsage:
    """Newest known context size of a session."""

    tokens: int
    # Identity of the transcript line the number came from — the watchdog
    # must not act twice on the same measurement, and a fresh line is the
    # only proof the session has moved on since the last `/compact`.
    line_id: str


def read_context_usage(transcript: Path) -> ContextUsage | None:
    """Return the context size reported by the newest assistant line, or
    None when the transcript is missing, unreadable, or has no usage yet.

    A `compact_boundary` written after that line supersedes it: the
    window has just been rebuilt from the summary and the next assistant
    call is what will reveal the real size. Until then the boundary's own
    `postTokens` is reported (0 when absent), keyed by the boundary line —
    otherwise the stale pre-compact number would keep tripping the
    watchdog and queue a second `/compact` on an already-compacted
    session (seen live 2026-10-29: two back-to-back 2-minute compactions).
    """
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # drop the partial first line
            tail = fh.read()
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        if _COMPACT_BOUNDARY in raw:
            boundary = _parse_compact_boundary(raw)
            if boundary is not None:
                return boundary
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        tokens = 0
        for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                tokens += value
        if tokens <= 0:
            continue
        line_id = entry.get("uuid")
        if not isinstance(line_id, str) or not line_id:
            line_id = str(entry.get("timestamp") or tokens)
        return ContextUsage(tokens=tokens, line_id=line_id)
    return None


_COMPACT_BOUNDARY = b'"compact_boundary"'
_SCAN_CHUNK = 256 * 1024


def _parse_compact_boundary(raw: bytes) -> ContextUsage | None:
    try:
        entry = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("subtype") != "compact_boundary":
        return None
    meta = entry.get("compactMetadata")
    post = meta.get("postTokens") if isinstance(meta, dict) else None
    tokens = post if isinstance(post, int) and not isinstance(post, bool) else 0
    line_id = entry.get("uuid")
    if not isinstance(line_id, str) or not line_id:
        line_id = f"boundary:{entry.get('timestamp') or tokens}"
    return ContextUsage(tokens=max(0, tokens), line_id=line_id)


def compact_boundary_since(transcript: Path, offset: int) -> bool:
    """True when a `compact_boundary` system line was appended after byte
    `offset` — how the auto-compact watchdog learns that the `/compact` it
    typed has finished (CC writes the boundary once the summary is in)."""
    try:
        with transcript.open("rb") as fh:
            fh.seek(max(0, offset))
            carry = b""
            while True:
                chunk = fh.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                if _COMPACT_BOUNDARY in carry + chunk:
                    return True
                carry = chunk[-len(_COMPACT_BOUNDARY) :]
    except OSError:
        return False


# How far back to look for a `/compact` that has not finished yet. A
# queued compaction can sit behind a long turn while the transcript grows
# with its tool output, so this is much larger than the usage tail.
_COMPACT_SCAN_BYTES = 8 * 1024 * 1024

# A `/compact` older than this without a boundary is treated as lost
# (CC crashed mid-compaction, the queued command was edited away) rather
# than as "still running" — otherwise the watchdog would never fire again
# on that session. Only meaningful for an idle session: a command queued
# behind a running turn is waiting, not lost, however old it is.
_MAX_PENDING_COMPACT_SEC = 45 * 60


def _compact_commands_newest_first(transcript: Path) -> Iterator[tuple[bytes, bool]]:
    """Yield `(raw_line, is_boundary)` for compaction markers in the
    transcript tail, newest first: `compact_boundary` system lines and
    queued or submitted `/compact` commands."""
    try:
        size = transcript.stat().st_size
        with transcript.open("rb") as fh:
            if size > _COMPACT_SCAN_BYTES:
                fh.seek(size - _COMPACT_SCAN_BYTES)
                fh.readline()  # drop the partial first line
            tail = fh.read()
    except OSError:
        return
    for raw in reversed(tail.splitlines()):
        if _COMPACT_BOUNDARY in raw:
            if _parse_compact_boundary(raw) is not None:
                yield raw, True
            continue
        if b"/compact" not in raw:
            continue
        try:
            entry = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and _is_compact_command(entry):
            yield raw, False


def compact_pending(
    transcript: Path,
    now: float | None = None,
    max_age_sec: float | None = _MAX_PENDING_COMPACT_SEC,
) -> bool:
    """True when the transcript's newest compaction marker is a `/compact`
    that has not produced its `compact_boundary` yet.

    Read from the transcript, not from memory, so it survives a bot
    restart and also sees compactions typed by hand into the pane — both
    were behind the 2026-10-30 double compaction: a reload wiped the
    in-process gate and the next watchdog tick queued a second `/compact`
    behind the still-pending first one. `now` is unix time for tests;
    `max_age_sec=None` disables the lost-compaction expiry (for a session
    whose turn is still running, so the queued command cannot have been
    lost yet — the 2026-10-31 double run was a 48-minute turn outliving
    the 45-minute window).
    """
    for raw, is_boundary in _compact_commands_newest_first(transcript):
        if is_boundary:
            return False  # boundary is newer than any pending command
        if max_age_sec is None:
            return True
        return _is_fresh(json.loads(raw), now, max_age_sec)
    return False


def last_compact_command_at(transcript: Path) -> float | None:
    """Unix time of the newest `/compact` in the transcript tail — queued
    or submitted, by the bot or by hand, finished or not. Backs the
    minimum spacing between automatic compactions: whatever the pending
    gate misses (an edited queue, a lost boundary), two compactions
    minutes apart are never what the operator wants."""
    for raw, is_boundary in _compact_commands_newest_first(transcript):
        if is_boundary:
            continue
        return _timestamp_of(json.loads(raw))
    return None


def _compact_text(content: object) -> bool:
    """`/compact` on its own or with custom instructions (`/compact <focus>`)."""
    return isinstance(content, str) and (
        content == "/compact" or content.startswith(("/compact ", "/compact\n"))
    )


def _is_compact_command(entry: dict[str, object]) -> bool:
    """A queued or submitted `/compact` — both shapes CC writes."""
    if entry.get("type") == "queue-operation" and entry.get("operation") == "enqueue":
        return _compact_text(entry.get("content"))
    if entry.get("type") == "user":
        message = entry.get("message")
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if _compact_text(content):
            return True
        return isinstance(content, list) and any(
            isinstance(b, dict) and _compact_text(b.get("text")) for b in content
        )
    return False


def _timestamp_of(entry: dict[str, object]) -> float | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_fresh(entry: dict[str, object], now: float | None, max_age_sec: float) -> bool:
    written = _timestamp_of(entry)
    if written is None or now is None:
        return True
    return now - written < max_age_sec
