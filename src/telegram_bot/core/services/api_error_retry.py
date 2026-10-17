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
import time
from collections.abc import Callable

from telegram_bot.core.types import ChannelKey

# Base delay before nudging the pane on the first attempt. CC has just
# rendered the error and returned to the input prompt; a short settle
# avoids racing the paste against the TUI redraw. Subsequent attempts
# back off exponentially (see `nudge_delay_sec`) — upstream re-sign /
# re-route incidents often need tens of seconds to clear, so hammering
# every 2s just burns the budget inside one broken window.
NUDGE_DELAY_SEC = 2.0

# Cap for the exponential backoff. 2 → 4 → 8 → 16 → 20 → 20 → ...
MAX_NUDGE_DELAY_SEC = 20.0


def nudge_delay_sec(attempt: int) -> float:
    """Exponential backoff for the given 1-based attempt, capped."""
    return min(NUDGE_DELAY_SEC * 2.0 ** (attempt - 1), MAX_NUDGE_DELAY_SEC)


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

    The budget resets on three signals:

    - ``note_progress`` — any successful stream event after an error
      means the incident healed, so the next incident gets a fresh
      budget. A new user turn through the bot also resets (see
      ``send_stream``).
    - **Incident window** — a new error arriving more than
      ``reset_after_sec`` after the previous one is a *new* incident,
      not a continuation of the exhausted one. This covers recovery
      paths the bot cannot observe: the user typing directly into the
      tmux pane after the budget ran out, or the upstream healing while
      no tail was attached. Within the window an exhausted budget stays
      exhausted, so a genuinely broken session still can't ping-pong
      ``continue`` forever.

    A ``limit`` of 0 disables auto-retry entirely.
    """

    def __init__(
        self,
        limit: int,
        reset_after_sec: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._reset_after_sec = reset_after_sec
        self._clock = clock
        # channel -> (consecutive attempts, monotonic time of last error)
        self._attempts: dict[ChannelKey, tuple[int, float]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def register_error(self, channel_key: ChannelKey) -> int | None:
        now = self._clock()
        prior, last_seen = self._attempts.get(channel_key, (0, now))
        if prior and now - last_seen > self._reset_after_sec:
            prior = 0  # quiet gap — new incident, fresh budget
        attempt = prior + 1
        if attempt > self._limit:
            # Keep the timestamp fresh: errors still firing back-to-back
            # are the same incident and must not sneak past the window.
            self._attempts[channel_key] = (prior, now)
            return None
        self._attempts[channel_key] = (attempt, now)
        return attempt

    def note_progress(self, channel_key: ChannelKey) -> None:
        self._attempts.pop(channel_key, None)
