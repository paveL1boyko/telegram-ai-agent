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
from dataclasses import dataclass
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
    None when the transcript is missing, unreadable, or has no usage yet."""
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
