"""Directory-listing backend for the /cwd folder picker.

The bare `/cwd` command posts an inline keyboard with the subdirectories of
the current cwd's parent, so a project switch is one tap instead of typing
an absolute path on a phone keyboard. Picker state (which directory is
being listed) lives here server-side; callback_data carries only the token,
mirroring `picker_store.py` for /resume.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)


def list_subdirs(base: Path) -> list[Path]:
    """Sorted non-hidden subdirectories of `base`; empty on any error."""
    try:
        entries = [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        logger.warning("cwd picker: cannot list %s", base, exc_info=True)
        return []
    return sorted(entries, key=lambda p: p.name.lower())


@dataclass(frozen=True)
class CwdPickerState:
    chat_id: int
    thread_id: int
    # Directory whose subdirectories the picker currently shows. The "⬆ .."
    # button moves this one level up; "✅ <name>" selects it as the new cwd.
    list_dir: Path
    created_at: float


class CwdPickerStore:
    """In-memory /cwd picker state; TTL is enforced lazily on access."""

    def __init__(self, *, ttl_sec: float = 900.0, clock: object = time.time) -> None:
        self._ttl_sec = ttl_sec
        self._clock = clock
        self._states: dict[str, CwdPickerState] = {}

    def put(self, state: CwdPickerState) -> str:
        while True:
            token = secrets.token_hex(4)
            if token not in self._states:
                self._states[token] = state
                return token

    def get(self, token: str) -> CwdPickerState | None:
        state = self._states.get(token)
        if state is None:
            return None
        now = self._clock()  # type: ignore[operator]
        if now - state.created_at > self._ttl_sec:
            self._states.pop(token, None)
            return None
        return state

    def move_up(self, token: str) -> CwdPickerState | None:
        """Point the picker at the parent of its current listing directory."""
        state = self.get(token)
        if state is None:
            return None
        parent = state.list_dir.parent
        if parent == state.list_dir:
            return state  # filesystem root — nowhere to go
        updated = replace(state, list_dir=parent)
        self._states[token] = updated
        return updated

    def drop(self, token: str) -> None:
        self._states.pop(token, None)
