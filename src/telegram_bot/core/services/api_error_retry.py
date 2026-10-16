"""Auto-retry for transient Claude Code API errors.

Claude Code writes a failed API request into the transcript jsonl as an
``assistant`` entry flagged ``isApiErrorMessage: true`` and stops the
turn — 400-class errors are deliberately not retried by the CLI itself
(only 429/5xx/network are). Some of those 400s are transient: the
canonical case is ``Invalid `signature` in `thinking` block`` — replayed
thinking blocks failing server-side signature validation after a model
switch or an upstream re-route through a proxy. Empirically the turn
continues fine after any follow-up user message ("continue").

This module holds the engine-agnostic pieces of that auto-nudge:

- ``RETRYABLE_API_ERROR_PATTERNS`` / ``is_retryable_api_error`` — which
  error texts are worth nudging. Deliberately narrow: a pattern belongs
  here only when a bare follow-up message is known to heal it. Rate
  limits (429) heal by waiting, auth errors don't heal at all — both
  must keep surfacing to the user as before.
- ``ApiErrorRetryTracker`` — per-channel consecutive-retry budget, so a
  genuinely broken session can never ping-pong ``continue`` forever.

The actual send lives in ``TmuxManager`` (it owns the pane and the
modal-guarded send path); see ``_handle_api_error_event`` there.
"""

from __future__ import annotations

import re

from telegram_bot.core.types import ChannelKey

# Delay before nudging the pane. CC has just rendered the error and
# returned to the input prompt; a short settle avoids racing the paste
# against the TUI redraw. Not user-tunable — it's a TUI timing constant,
# not a policy knob.
NUDGE_DELAY_SEC = 2.0

# Error texts that a bare follow-up message is known to heal. Matched
# case-insensitively anywhere in the text, so proxy prefixes like
# "Upstream provider rejected the request." still match. Backticks are
# optional — CC has rendered the message both with and without them.
RETRYABLE_API_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"invalid `?signature`? in `?thinking`? block", re.IGNORECASE),
)


def is_retryable_api_error(text: str) -> bool:
    """True when the API error text matches a known-transient pattern."""
    return any(pattern.search(text) for pattern in RETRYABLE_API_ERROR_PATTERNS)


class ApiErrorRetryTracker:
    """Consecutive-retry budget per channel.

    ``register_error`` increments the channel's counter and returns the
    1-based attempt number while within the limit, or ``None`` once the
    budget is exhausted (the caller then surfaces the raw error text, as
    the bot always did before this feature).

    ``note_progress`` resets the counter: any successful stream event
    after an error means the incident healed, so the next incident gets
    a fresh budget. A new user turn also resets (see ``send_stream``).

    A ``limit`` of 0 disables auto-retry entirely.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._attempts: dict[ChannelKey, int] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def register_error(self, channel_key: ChannelKey) -> int | None:
        attempt = self._attempts.get(channel_key, 0) + 1
        if attempt > self._limit:
            return None
        self._attempts[channel_key] = attempt
        return attempt

    def note_progress(self, channel_key: ChannelKey) -> None:
        self._attempts.pop(channel_key, None)
