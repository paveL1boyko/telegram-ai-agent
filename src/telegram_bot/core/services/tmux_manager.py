"""TmuxManager — persistent Claude Code TUI sessions inside tmux.

Facade over the helper modules:

- `tmux_state.py`   — dataclass, atomic JSON state store (with write-lock),
                      orphan scan.
- `tmux_spawn.py`   — async / sync tmux spawn, pane-width query, aliveness
                      check, session-name builder. All subprocess calls in
                      async paths are wrapped in `asyncio.to_thread` (Wave 3
                      B4 fix).
- `tmux_modal_watchdog.py` — background task that posts idle-modal alerts.
- `tmux_recovery.py`       — bot-startup restore / resume_tails / recovery
                              tail loop.

Runtime contract (Wave 2 TUI):
  - `_spawn_tmux` → `tmux new-session -d -s <name> claude --session-id <uuid>
     --dangerously-skip-permissions ...` + `await_prompt_ready`.
  - Transcript path is derived via `tui.paths.transcript_path(cwd, session_id)`
    and polled for existence against a shared 30 s clock (Decision 7).
  - Writes to CC go through `tui.send_keys.send_text_to_tmux`; Escape / /clear
    are issued as separate `subprocess.run(["tmux", "send-keys", ...])`
    invocations with list-args (no shell=True).
  - `send_direct` and `send_stream` both route user prompts through
    `_safe_send_and_enter` (Wave 3 B1 fix) — capture → send-keys -l →
    poll-until-seen → pre-Enter re-capture → Enter. A modal-blocked prompt
    triggers a Telegram alert and the helper returns False; callers roll
    back the per-turn UI (command acknowledgement, live buffer).
  - `clear_context` kills the tmux session and respawns CC with a fresh
    UUID4 passed via `--session-id`.
  - `switch_session` returns bool — False when the target transcript is
    missing on disk.
  - `restore_all` / `resume_tails` live in `tmux_recovery.py`.

All `subprocess.run` invocations use list-args to defeat command injection.
`subprocess`, `capture_pane`, `send_text_to_tmux`, `send_enter`,
`await_prompt_ready` are imported at module level so tests can continue to
patch them via `telegram_bot.core.services.tmux_manager.<name>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from telegram_bot.core.messages import t
from telegram_bot.core.services.api_error_retry import (
    ApiErrorRetryTracker,
    is_retryable_api_error,
)
from telegram_bot.core.services.api_error_retry import (
    nudge_delay_sec as _api_error_nudge_delay_sec,
)
from telegram_bot.core.services.bot_mcp_runtime import ensure_bot_runtime_mcp_config
from telegram_bot.core.services.claude import Mode, StreamEvent
from telegram_bot.core.services.providers import (
    CODEX_ADAPTER,
    NESSY_ADAPTER,
    ProviderAdapter,
    engine_display_name,
    get_adapter,
)
from telegram_bot.core.services.tail_runner import TailRunner
from telegram_bot.core.services.tmux_modal_watchdog import (
    ModalWatchdog,
)
from telegram_bot.core.services.tmux_modal_watchdog import (
    send_modal_alert as _send_modal_alert_impl,
)
from telegram_bot.core.services.tmux_modal_watchdog import (
    send_modal_idle_alert as _send_modal_idle_alert_impl,
)
from telegram_bot.core.services.tmux_modal_watchdog import (
    send_permission_autoapprove_notice as _send_permission_notice_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    build_resume_startup_cmd,
)
from telegram_bot.core.services.tmux_recovery import (
    restore_all as _restore_all_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    resume_tails as _resume_tails_impl,
)
from telegram_bot.core.services.tmux_recovery import (
    run_recovery_tail as _run_recovery_tail_impl,
)
from telegram_bot.core.services.tmux_spawn import (
    MODAL_WATCHDOG_INTERVAL_SEC as _MODAL_WATCHDOG_INTERVAL_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    SPAWN_READINESS_BUDGET_SEC as _SPAWN_READINESS_BUDGET_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    TMUX_NEW_SESSION_RETRY_DELAY_SEC as _TMUX_NEW_SESSION_RETRY_DELAY_SEC,
)
from telegram_bot.core.services.tmux_spawn import (
    TMUX_NEW_SESSION_TRANSIENT_ERRORS as _TMUX_NEW_SESSION_TRANSIENT_ERRORS,
)
from telegram_bot.core.services.tmux_spawn import (
    file_size as _file_size_fn,
)
from telegram_bot.core.services.tmux_spawn import (
    label_tmux_session,
    make_session_name,
    spawn_tmux_sync,
)
from telegram_bot.core.services.tmux_spawn import (
    tmux_alive as _tmux_alive_fn,
)
from telegram_bot.core.services.tmux_state import (
    StateStore,
    TmuxSessionState,
    _normalize_state_dict,  # noqa: F401 — re-exported for legacy test imports
    scan_orphan_tmux_sessions,
)
from telegram_bot.core.services.tmux_state import (
    peek_saved_session as _peek_saved_session_impl,
)
from telegram_bot.core.services.topic_config import Engine, TopicConfig
from telegram_bot.core.services.topic_runtime import (
    BotDefaults,
    TopicRuntimeConfig,
    resolve_topic_runtime_config,
)
from telegram_bot.core.tui.capture import await_prompt_ready
from telegram_bot.core.tui.context_usage import (
    compact_boundary_since,
    compact_pending,
    read_context_usage,
)
from telegram_bot.core.tui.modal_detect import (
    capture_pane,
    claude_input_bar_content,
    codex_input_bar_content,
    codex_prompt_visible_in_pane,
    is_modal_present,
    nessy_input_bar_content,
)
from telegram_bot.core.tui.paths import generate_session_uuid, transcript_path
from telegram_bot.core.tui.permission_prompt import (
    normalize_tool_name,
    parse_permission_prompt,
)
from telegram_bot.core.tui.send_keys import (
    PASTE_MODE_CHAR_THRESHOLD,
    send_ctrl_u,
    send_enter,
    send_paste,
    send_text_to_tmux,
)
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

# How many trailing pane lines to dump alongside `BLOCKED reason=modal` /
# `RACE modal-before-enter` events. Wide enough to include the input bar,
# the bottom separator, and the modal footer if present (CC modals render
# their footer within ~3 lines of the pane bottom; idle bar sandwich is
# ~5 lines tall). 30 covers both with headroom for wrapped multi-line
# prompts without bloating journald.
_MODAL_DIAG_PANE_TAIL_LINES = 30


# Tail-loop constants live in tail_runner.py (W2.3 extraction).
_POLL_STEP_SEC = 0.05  # fine-grained poll interval inside send_direct's pane-verify loop
_ENTER_RETRY_SETTLE_SEC = 0.5
_ENTER_RETRY_LIMIT = 3

# Settle pause between pressing Enter on a verified tool-permission dialog
# and the re-capture that confirms the dialog actually dismissed
# (`_auto_approve_permission`). CC needs a frame to resolve the approval
# and redraw; 0.7s is comfortably above a render tick and below the modal
# watchdog interval, so a failed dismissal still surfaces as an idle alert
# on the next tick.
_PERMISSION_RESOLVE_WAIT_SEC = 0.7

# How many Enter attempts `_auto_approve_permission` makes before giving
# up. A single Enter can be lost when the capture catches the dialog
# mid-render (menu visible, footer not yet painted — keystroke swallowed,
# seen live on 2026-10-27). Each retry re-verifies the same-tool dialog on
# a fresh capture before pressing again, so the safety invariant (Enter
# only into a structurally verified dialog) holds on every attempt.
_PERMISSION_ENTER_ATTEMPTS = 3

# Auto-compact: how often `_resume_after_compact` looks for the
# compact_boundary, how long the pending gate / resume wait before assuming
# the `/compact` was lost (long — a compact typed into a busy session is
# queued behind the running turn, which can itself take many minutes), and
# the pause between a compact's Enter and the next paste so CC consumes
# the first before the second lands.
_AUTO_COMPACT_RESUME_POLL_SEC = 5.0
_AUTO_COMPACT_RESUME_DEADLINE_SEC = 30 * 60
_AUTO_COMPACT_RESUME_SETTLE_SEC = 3.0

# How often the activity watchdog stats each session's transcript for growth
# beyond the delivered offset. 10s keeps tmux-typed turns near-realtime in
# Telegram while staying far below the cost of the modal watchdog's
# capture-pane probe (a stat() per session per tick is effectively free).
_ACTIVITY_WATCHDOG_INTERVAL_SEC = 10.0

# Codex `_safe_send_codex` paste-retry budget. Three attempts x 1.5s poll
# each — the upper bound when codex is mid-render and the bracketed-paste
# chip needs an extra frame to land. Beyond this we surface a modal alert
# rather than silently looping forever.
_CODEX_PASTE_RETRY_LIMIT = 3
_CODEX_PASTE_POLL_BUDGET_SEC = 1.5
_CODEX_PASTE_POLL_STEP_SEC = 0.1

# `_send_with_modal_guard` settle pauses. Bracketed paste lands as one
# event in Claude TUI, but the renderer needs a frame to absorb it before
# Enter — without a settle, Enter races the paste and either triggers
# nothing (paste still buffering) or submits the previous bar content.
# Long/multiline payloads need more time to render; short single-line
# get a smaller window so latency stays low.
#
# Threshold is aliased to `PASTE_MODE_CHAR_THRESHOLD` from `send_keys` —
# both name the Claude TUI paste-mode trigger; keeping them linked
# prevents drift if the TUI changes its threshold.
_GUARD_LONG_PROMPT_THRESHOLD = PASTE_MODE_CHAR_THRESHOLD
_GUARD_SETTLE_LONG_SEC = 0.5
_GUARD_SETTLE_SHORT_SEC = 0.25

# Probe-input readiness gate. After the prompt glyph (codex `>` / claude `>` markers) appears we
# still cannot trust that the underlying ratatui (or similar) event
# loop is processing paste/keypress events: codex demonstrably needs
# ~5-8s extra after the prompt is rendered to finish loading MCP
# servers, during which any paste lands silently in the pty but is
# not echoed back to the pane. The probe sends one literal `.` and
# watches the input bar grow; only then is the session declared ready.
# Backspace clears the probe afterwards.
_PROBE_CHAR = "."
_PROBE_INPUT_READY_BUDGET_SEC = 30.0
_PROBE_INPUT_READY_RENDER_WAIT_SEC = 0.3
_PROBE_INPUT_READY_RETRY_DELAY_SEC = 0.5

__all__ = [
    "SwitchResult",
    "TmuxManager",
    "TmuxSessionState",
    "scan_orphan_tmux_sessions",
]


@dataclass(frozen=True)
class SwitchResult:
    kind: Literal[
        "switched",
        "started",
        "already_on_it",
        "target_missing",
        "invalid_id",
        "spawn_failed",
        "config_write_failed",
    ]
    engine_changed: bool = False
    mode_changed: bool = False
    # Model override the resumed session was actually started with — None
    # means the CLI default. Lets the resume notice show whether a [1m]
    # override survived the switch.
    model: str | None = None


class TmuxManager:
    """Manages persistent CC TUI sessions inside tmux windows."""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        api_error_retry_limit: int = 10,
        api_error_retry_prompt: str = "continue",
        api_error_retry_reset_sec: float = 300.0,
        auto_approve_permission_tools: Iterable[str] | None = None,
    ) -> None:
        self._sessions_dir = sessions_dir
        self._sessions: dict[ChannelKey, TmuxSessionState] = {}
        self._cancel_events: dict[ChannelKey, asyncio.Event] = {}
        self._is_processing: dict[ChannelKey, bool] = {}
        # Auto-retry budget for transient CC API errors ("Invalid `signature`
        # in `thinking` block" & co). The tracker counts consecutive nudges
        # per channel; `_handle_api_error_event` performs the nudge itself.
        self._api_error_retry = ApiErrorRetryTracker(
            api_error_retry_limit, reset_after_sec=api_error_retry_reset_sec
        )
        self._api_error_retry_prompt = api_error_retry_prompt
        # Operator pre-authorization for CC tool-permission dialogs (see
        # tui/permission_prompt.py). Normalized names ("websearch"); the
        # modal watchdog answers "1. Yes" only when the dialog verifies
        # structurally AND the tool is in this set. Empty = disabled.
        self._auto_approve_tools = {
            normalize_tool_name(name) for name in (auto_approve_permission_tools or ())
        }
        # Shared spawn deadline per channel, populated by _spawn_tmux and
        # consumed by the next _tail_until_done to bound the transcript
        # poll-for-existence window inside the same 30s clock as readiness
        # (Decision 7). Cleared once the tail sees the file.
        self._spawn_deadlines: dict[ChannelKey, float] = {}
        self._tui_start_snapshots: dict[ChannelKey, tuple[set[Path], float]] = {}
        # Channels whose most recent _spawn_tmux passed prompt-readiness but
        # failed the input probe (probe budget elapsed without the test char
        # appearing in the input bar). Set by `_spawn_tmux`, consumed by
        # `start_session` to skip kill-on-failure and register the session
        # anyway with `session_id=None`. Runtime-only — not persisted in
        # state.json. After bot restart, `restore_all` reattaches the tmux
        # session as a normal one; the modal watchdog will re-discover any
        # still-open modal on its next tick.
        self._probe_blocked: set[ChannelKey] = set()
        # LiveStatusBuffer per channel — owned by TmuxManager because the tail
        # loop outlives a single user message. Typed as object to avoid a
        # circular import on LiveStatusBuffer.
        self._buffers: dict[ChannelKey, object] = {}
        self._buffer_lock = asyncio.Lock()
        # Live-buffer plumbing, wired in at startup via wire_live_buffer().
        self._bot: object | None = None
        self._topic_config: object | None = None
        self._recovery_on_event_factory: (
            Callable[[ChannelKey], Callable[[StreamEvent], Awaitable[None] | None]] | None
        ) = None
        # Activity watchdog task — see start_activity_watchdog.
        self._activity_watchdog_task: asyncio.Task[None] | None = None
        # Auto-compact watchdog — see start_auto_compact_watchdog. `_seen`
        # remembers which transcript line triggered the last `/compact` per
        # channel, so one oversized measurement cannot fire twice;
        # `_pending` holds (transcript offset, monotonic deadline) from the
        # moment a `/compact` was typed until its compact_boundary lands —
        # a busy session keeps producing new oversized lines every tick,
        # and without this gate each tick would queue one more `/compact`
        # behind the running turn.
        self._auto_compact_task: asyncio.Task[None] | None = None
        self._auto_compact_default_tokens = 0
        self._auto_compact_continue_prompt = ""
        self._auto_compact_seen: dict[ChannelKey, str] = {}
        self._auto_compact_pending: dict[ChannelKey, tuple[int, float]] = {}
        # Strong refs to in-flight `_resume_after_compact` tasks (asyncio
        # keeps only weak refs to tasks).
        self._auto_compact_resume_tasks: set[asyncio.Task[None]] = set()
        # Per-channel lock around send_direct's capture→send-keys→verify→Enter
        # sequence. Without it, two overlapping user messages race on shared
        # pane state.
        self._channel_locks: dict[ChannelKey, asyncio.Lock] = {}
        self._state_store = StateStore(sessions_dir / "state.json")
        # Modal watchdog state. `_last_modal_pane` is the dedup key — the
        # pane snapshot we last alerted on for a channel. Shared with
        # `_send_modal_alert` so user-initiated and watchdog-initiated
        # alerts de-duplicate against each other.
        self._last_modal_pane: dict[ChannelKey, str] = {}
        # Tracks the Telegram message_id of the currently visible TUI panel
        # (either a manual /tui panel or a modal alert) for each channel.
        # Used by the TUI button toggle to close the active panel instead of
        # opening a second one. Updated by both _handle_tail_entry (tail.py)
        # and send_modal_alert / send_modal_idle_alert (tmux_modal_watchdog.py).
        self._tui_panel_messages: dict[ChannelKey, int] = {}
        # Re-resolve `_check_channel_modal` on every tick so `patch.object(
        # mgr, "_check_channel_modal", ...)` in tests affects the running
        # loop; otherwise the method captured at ModalWatchdog init would
        # shadow any later attribute replacement.
        self._modal_watchdog = ModalWatchdog(
            check_channel=lambda key: self._check_channel_modal(key),
            channels_snapshot=lambda: list(self._sessions.keys()),
        )

    # --- Backwards-compatible accessors ---

    @property
    def _state_path(self) -> Path:
        """Path to the persisted state.json (read-only).

        Kept as a property for tests/callers that still access
        `mgr._state_path` directly — the actual I/O goes through
        `self._state_store`.
        """
        return self._state_store.path

    @property
    def _modal_watchdog_task(self) -> asyncio.Task[None] | None:
        """Expose the watchdog task for tests. Read-only in production."""
        return self._modal_watchdog._task

    @_modal_watchdog_task.setter
    def _modal_watchdog_task(self, value: asyncio.Task[None] | None) -> None:
        self._modal_watchdog._task = value

    def set_tui_panel(self, key: ChannelKey, message_id: int) -> None:
        self._tui_panel_messages[key] = message_id

    def pop_tui_panel(self, key: ChannelKey) -> int | None:
        return self._tui_panel_messages.pop(key, None)

    def reset_modal_dedup(self, key: ChannelKey) -> None:
        """Clear the watchdog's modal-pane dedup for a channel.

        Used when a modal-alert message is deleted after a choice-button
        press: if the keypress did NOT actually dismiss the modal (wrong
        digit for the menu), the watchdog must re-alert on its next tick
        instead of staying silenced by the stale dedup entry.
        """
        self._last_modal_pane.pop(key, None)

    def wire_live_buffer(self, *, bot: object, topic_config: object) -> None:
        """Attach the services needed to materialize LiveStatusBuffers.

        Called once at startup after Bot and TopicConfig are built.
        """
        self._bot = bot
        self._topic_config = topic_config

    def live_buffer_available(self) -> bool:
        """True if wire_live_buffer() has been called — buffers can be built."""
        return self._bot is not None and self._topic_config is not None

    def get_live_bot(self) -> object | None:
        return self._bot

    def wire_recovery_events(
        self,
        factory: Callable[[ChannelKey], Callable[[StreamEvent], Awaitable[None] | None]],
    ) -> None:
        """Attach the Telegram-delivery factory used by recovery tails.

        Separate from `resume_tails` (which also starts a permanent tail per
        session) so the activity watchdog can deliver tmux-typed output
        without reviving the tail-per-session-at-boot design.
        """
        self._recovery_on_event_factory = factory

    def get_topic_config(self) -> object | None:
        return self._topic_config

    # --- Public API ---

    def get_buffer(self, channel_key: ChannelKey) -> object | None:
        """Return the current LiveStatusBuffer for a channel, if any."""
        return self._buffers.get(channel_key)

    async def set_buffer(self, channel_key: ChannelKey, new_buffer: object) -> None:
        """Install a new LiveStatusBuffer, closing any existing one atomically.

        Atomic under self._buffer_lock so the tail's sender never ends up
        with a mix of old-and-new buffer references when the user fires a
        second prompt while CC is still working on the first.
        """
        async with self._buffer_lock:
            old = self._buffers.get(channel_key)
            self._buffers[channel_key] = new_buffer
        if old is not None:
            close = getattr(old, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def close_buffer(self, channel_key: ChannelKey) -> None:
        """Close and forget the buffer for a channel. Idempotent."""
        async with self._buffer_lock:
            old = self._buffers.pop(channel_key, None)
        if old is None:
            return
        close = getattr(old, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()

    def is_processing(self, channel_key: ChannelKey) -> bool:
        """True if CC is actively processing a prompt for this channel."""
        return self._is_processing.get(channel_key, False)

    def is_active(self, channel_key: ChannelKey) -> bool:
        """True if this channel has a live tmux session."""
        if channel_key not in self._sessions:
            return False
        state = self._sessions[channel_key]
        return self._tmux_alive(state.session_name)

    def get_session_id(self, channel_key: ChannelKey) -> str | None:
        """Return current CC session_id for a channel."""
        state = self._sessions.get(channel_key)
        return state.session_id if state else None

    @staticmethod
    def expected_epoch(state: TmuxSessionState) -> str:
        """8-hex epoch for inline-keyboard callback_data binding.

        When the session has a real CC session_id, the first 8 hex chars
        are the keyboard epoch (as before). When session_id is None — the
        codex cold-start case where input is blocked by a startup-modal
        and codex hasn't yet written its session_meta jsonl — derive a
        stable 8-hex synthetic epoch from the tmux session_name. Same
        format as a real epoch, passes the `[0-9a-f]{8}` regex in
        `tail_keyboard.parse_tail_callback`. The synthetic epoch is
        deterministic per channel, so a `/tui` keyboard built before the
        real session_id materialises stays valid across re-renders; once
        the real session_id arrives, the old synthetic keyboard becomes
        stale (epoch mismatch) and the user is prompted to call /tui
        again — same UX as after `/new`.
        """
        if state.session_id:
            return state.session_id[:8]
        # Non-cryptographic — just a stable derivation per session_name.
        # `usedforsecurity=False` silences Bandit B324.
        return hashlib.sha1(state.session_name.encode(), usedforsecurity=False).hexdigest()[:8]

    def get_expected_epoch(self, channel_key: ChannelKey) -> str | None:
        """Public helper for callback handlers: epoch for an active session.

        Returns None when there is no registered session for the channel.
        For registered sessions, falls back to a synthetic epoch when
        the underlying CC session_id is not yet known (startup-modal
        blocked codex cold-start). See `expected_epoch` for details.
        """
        state = self._sessions.get(channel_key)
        return self.expected_epoch(state) if state else None

    def get_session_snapshot(
        self, channel_key: ChannelKey
    ) -> tuple[str, str | None, str | None] | None:
        """Atomic (session_id, provider, model) snapshot, or None if no session.

        Single dict lookup eliminates the TOCTOU window between separate
        ``get_session_id`` and ``get_provider_model`` calls. Use this from
        record-message callers (tmux flow): if the session is wiped between
        the two calls, a split lookup can record a tmux-originating session_id
        with the wrong provider — exactly the failure mode this fix exists to
        prevent. Returns ``None`` (not a tuple of Nones) so callers express
        the missing-session check with one branch.
        """
        state = self._sessions.get(channel_key)
        if state is None or state.session_id is None:
            return None
        return state.session_id, state.provider, state.model

    def get_active_session_id(self, channel_key: ChannelKey) -> str | None:
        """Return current TUI session_id for picker UX."""
        return self.get_session_id(channel_key)

    def get_session_name(self, channel_key: ChannelKey) -> str | None:
        """Return current tmux session-name for a channel (e.g. `cc-1-0`)."""
        state = self._sessions.get(channel_key)
        return state.session_name if state else None

    def update_session_model(self, channel_key: ChannelKey, model: str | None) -> bool:
        """Record a new model on the live session after an in-TUI `/model` switch.

        Keeps the respawn paths (`clear_context`, `switch_session`) and the
        persisted state.json in agreement with what the running TUI actually
        uses. Returns False when no session is registered for the channel.
        """
        state = self._sessions.get(channel_key)
        if state is None:
            return False
        state.model = model
        self._save_state()
        return True

    def peek_saved_session(self, channel_key: ChannelKey, cwd: str) -> str | None:
        """Return a safely-resumable session_id from state.json, or None.

        See `tmux_state.peek_saved_session` for the full guard set. Does
        not mutate state — safe to call on every user message.
        """
        return _peek_saved_session_impl(self._state_store, channel_key, cwd)

    async def start_session(
        self,
        channel_key: ChannelKey,
        *,
        mode: Mode,
        cwd: str,
        mcp_config: str,
        chat_id: int,
        session_manager: object,
        resume_session_id: str | None = None,
        provider: str = "claude",
        model: str | None = None,
    ) -> None:
        """Create (or resume) a tmux session with a persistent CC TUI process."""
        name = self._make_name(channel_key, provider=provider)
        session_dir = self._sessions_dir / name
        base_mcp_config = mcp_config
        mcp_config = self._ensure_runtime_mcp_config(
            channel_key=channel_key,
            base_mcp_config=base_mcp_config,
            session_dir=session_dir,
            session_manager=session_manager,
        )
        transcript_abs: str | None = None

        if provider == "codex":
            if resume_session_id is not None:
                session_id = resume_session_id
                startup_cmd = CODEX_ADAPTER.build_tui_resume(
                    cwd=cwd,
                    session_id=resume_session_id,
                    model=model,
                    mcp_config=mcp_config,
                )
                current_state = self._sessions.get(channel_key)
                saved_path = CODEX_ADAPTER.transcript_path_for_state(
                    cwd=cwd,
                    session_id=resume_session_id,
                    transcript_path=current_state.transcript_path if current_state else None,
                )
                initial_offset = self._file_size(saved_path) if saved_path else 0
                transcript_abs = str(saved_path) if saved_path else None
            else:
                existing = set(Path.home().joinpath(".codex", "sessions").glob("**/*.jsonl"))
                since_wall = time.time()
                session_id = None
                startup_cmd = CODEX_ADAPTER.build_tui_start(
                    cwd=cwd,
                    model=model,
                    mcp_config=mcp_config,
                )
                initial_offset = 0
        elif provider == "nessy":
            if resume_session_id is not None:
                session_id = resume_session_id
                startup_cmd = NESSY_ADAPTER.build_tui_resume(
                    cwd=cwd,
                    session_id=resume_session_id,
                    model=model,
                    mcp_config=mcp_config,
                )
                current_state = self._sessions.get(channel_key)
                saved_path = NESSY_ADAPTER.transcript_path_for_state(
                    cwd=cwd,
                    session_id=resume_session_id,
                    transcript_path=current_state.transcript_path if current_state else None,
                )
                initial_offset = self._file_size(saved_path) if saved_path else 0
                transcript_abs = str(saved_path) if saved_path else None
            else:
                existing = set(Path.home().joinpath(".nessy", "sessions").glob("**/*.jsonl"))
                since_wall = time.time()
                session_id = None
                startup_cmd = NESSY_ADAPTER.build_tui_start(
                    cwd=cwd,
                    model=model,
                    mcp_config=mcp_config,
                )
                initial_offset = 0
        elif resume_session_id is not None:
            session_id = resume_session_id
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=mode,
                mcp_config=mcp_config,
                resume_session_id=resume_session_id,
                model=model,
            )
            # Seek past all events already in the transcript — they were
            # delivered to Telegram in the previous run. offset=0 would
            # re-emit every historical event and flood the user with duplicates.
            initial_offset = self._file_size(transcript_path(cwd, session_id))
        else:
            session_id = generate_session_uuid()
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=mode,
                mcp_config=mcp_config,
                session_id_new=session_id,
                model=model,
            )
            initial_offset = 0

        state = TmuxSessionState(
            session_name=name,
            session_dir=str(session_dir),
            session_id=session_id,
            mode=mode,
            cwd=cwd,
            mcp_config=mcp_config,
            chat_id=chat_id,
            offset=initial_offset,
            runner_version=get_adapter(provider).runner_version_tag(),
            provider=provider,
            model=model,
            transcript_path=transcript_abs,
            base_mcp_config=base_mcp_config,
        )

        # User-facing loading status: codex/claude TUI cold-start can take
        # 5-8 seconds (MCP server load + ratatui input handler wiring).
        # Without a visible signal the user thinks the bot is hung and
        # spams retries — which we then have to dedupe with paste-retry
        # double-checks. A loading message with edit-on-completion is
        # cheap UX that closes the perception gap.
        loading_msg = await self._post_engine_loading_message(channel_key, provider)

        try:
            await self._spawn_tmux(
                name=name,
                session_dir=session_dir,
                cwd=cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=provider,
            )
        except Exception as exc:
            await self._edit_engine_loading_message(
                loading_msg,
                t("ui.engine_start_failed", engine=provider, exc=str(exc)[:200]),
            )
            raise

        # `_spawn_tmux` may have succeeded structurally (tmux up + prompt
        # ready) but the input probe failed — typically because a startup
        # modal is blocking the input handler. In that case tmux is left
        # alive (see _spawn_tmux) and the channel is flagged in
        # `_probe_blocked`. Register the session anyway so /tui passes
        # `is_active(key)` and the modal watchdog sees it; pick a
        # different loading-message text so the user knows to open /tui.
        is_probe_blocked = channel_key in self._probe_blocked
        if is_probe_blocked:
            await self._edit_engine_loading_message(
                loading_msg,
                t("ui.engine_started_input_blocked", engine=provider),
            )
        else:
            await self._edit_engine_loading_message(
                loading_msg, t("ui.engine_ready", engine=provider)
            )

        # The transcript discovery snapshot must be saved on BOTH
        # the happy path AND the probe-blocked path. After the user
        # dismisses the modal through /tui and sends their first real
        # message, `_locate_transcript_after_send` diffs the
        # post-send jsonl listing against this snapshot to find the new
        # session_id. Without it, that lookup would scan an empty
        # baseline and pick up unrelated transcripts.
        _adapter = get_adapter(provider)
        if _adapter.generates_own_session_id() and resume_session_id is None:
            if provider == "nessy":
                self._tui_start_snapshots[channel_key] = NESSY_ADAPTER.transcript_snapshot()
            else:
                self._tui_start_snapshots[channel_key] = (existing, since_wall)

        self._sessions[channel_key] = state
        self._save_state()
        # Now that the session is registered, drop the probe-blocked flag.
        # Future spawns (clear_context / new) start fresh; the flag only
        # bridges the in-progress _spawn_tmux → start_session handoff.
        self._probe_blocked.discard(channel_key)
        logger.info(
            "Started tmux session %s for channel %s "
            "(resume=%s, session_id=%s, offset=%d, probe_blocked=%s)",
            name,
            channel_key,
            resume_session_id is not None,
            session_id,
            initial_offset,
            is_probe_blocked,
        )

    async def _spawn_tmux(
        self,
        *,
        name: str,
        session_dir: Path,
        cwd: str,
        startup_cmd: list[str],
        channel_key: ChannelKey | None = None,
        provider: str = "claude",
    ) -> None:
        """Spawn a tmux session running `claude` TUI and await prompt readiness.

        - Kills any previous tmux session with the same name.
        - Records a shared 30s deadline used by both `await_prompt_ready`
          and the subsequent transcript poll-for-existence (Decision 7).
        - Wave 3 B4: wraps all sync `subprocess.run` in `asyncio.to_thread`
          so the tmux server handshake no longer blocks the event loop
          (20-80 ms on idle, seconds under load).
        - On readiness-timeout `await_prompt_ready` already kills the
          session; we translate that to `RuntimeError` for the caller.
        """
        session_dir.mkdir(parents=True, exist_ok=True)
        # Clear any leftover probe-blocked flag from a previous spawn on
        # this channel before we start fresh. Other callers of
        # `_spawn_tmux` (clear_context, switch_session, _swap_session_id)
        # don't read the flag themselves, so without this discard a
        # stale flag from a prior session could leak into the next
        # `start_session` and falsely claim "blocked" when the probe
        # actually succeeded this time around.
        if channel_key is not None:
            self._probe_blocked.discard(channel_key)
        await asyncio.to_thread(
            subprocess.run, ["tmux", "kill-session", "-t", f"={name}"], capture_output=True
        )

        spawn_start = time.monotonic()
        deadline = spawn_start + _SPAWN_READINESS_BUDGET_SEC

        new_session_argv = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            "200",
            "-y",
            "50",
            *startup_cmd,
        ]
        logger.info(
            "TUI_IO: spawning tmux session=%s cmd=%s cwd=%s",
            name,
            " ".join(new_session_argv[1:]),
            cwd,
        )
        result = await asyncio.to_thread(
            subprocess.run, new_session_argv, capture_output=True, text=True, cwd=cwd
        )
        logger.info(
            "TUI_IO: tmux new-session result=%d stdout=%r stderr=%r",
            result.returncode,
            result.stdout[:200] if result.stdout else "",
            result.stderr[:200] if result.stderr else "",
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # When `kill-session` removed the last session on the server,
            # tmux shuts the server down (exit-empty on); a racing
            # `new-session` sees "server exited unexpectedly". Retry once.
            if any(marker in stderr for marker in _TMUX_NEW_SESSION_TRANSIENT_ERRORS):
                logger.warning(
                    "tmux new-session transient failure for %s (%r); retrying once",
                    name,
                    stderr,
                )
                await asyncio.sleep(_TMUX_NEW_SESSION_RETRY_DELAY_SEC)
                result = await asyncio.to_thread(
                    subprocess.run,
                    new_session_argv,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                )
                stderr = result.stderr.strip()
            if result.returncode != 0:
                raise RuntimeError(f"tmux new-session failed: {stderr}")

        await asyncio.to_thread(label_tmux_session, name, self._topic_name(channel_key))

        # Check if session actually exists after new-session
        session_exists = await asyncio.to_thread(
            subprocess.run, ["tmux", "has-session", "-t", f"={name}"], capture_output=True
        )
        logger.info(
            "TUI_IO: session_exists=%d for %s",
            session_exists.returncode,
            name,
        )

        remaining = max(deadline - time.monotonic(), 0.0)
        if provider == "codex":
            ready = await self._await_codex_prompt_ready(name, timeout=remaining)
        else:
            ready = await await_prompt_ready(name, timeout=remaining, clock=time.monotonic)
        if not ready:
            await asyncio.to_thread(
                subprocess.run, ["tmux", "kill-session", "-t", f"={name}"], capture_output=True
            )
            raise RuntimeError("CC TUI start timeout")

        # Second readiness gate: the prompt glyph (codex single-angle /
        # claude heavy-angle / nessy plain ASCII `>`) is visible, but is
        # the input handler actually wired up? Codex in particular needs
        # ~5-8s after the glyph appears to finish loading MCP servers;
        # sending a paste during that window is silently lost. Probe
        # with one dot and wait for it to land in the input bar.
        bar_fn_by_provider: dict[str, Callable[[str], str | None]] = {
            "codex": codex_input_bar_content,
            "nessy": nessy_input_bar_content,
            "claude": claude_input_bar_content,
        }
        get_bar_fn = bar_fn_by_provider.get(provider, claude_input_bar_content)
        ready_input = await self._probe_input_ready(name, get_input_bar_fn=get_bar_fn)
        if not ready_input:
            # Don't kill tmux. A failed probe is the strongest universal
            # signal that input is blocked — almost always a startup
            # modal (codex update prompt, trust dialog, accept-terms,
            # any future codex/claude release with a new modal). The
            # text-based whitelist in `is_modal_present` cannot keep up
            # with new modals, but the empirical "the dot never landed"
            # signal is universal. Keep tmux alive so the user can see
            # the pane through `/tui` and dismiss the modal manually;
            # the modal watchdog will also discover it on the next tick.
            # `start_session` checks this set and registers the session
            # in `_sessions` with `session_id=None` so /tui passes its
            # is_active guard.
            if channel_key is not None:
                self._probe_blocked.add(channel_key)
            logger.warning(
                "TUI_IO: probe_failed_session_kept session=%s channel=%s",
                name,
                channel_key,
            )

        if channel_key is not None:
            # Hand the remaining budget to the next _tail_until_done so its
            # transcript poll-for-existence shares the same clock.
            self._spawn_deadlines[channel_key] = deadline

    async def _await_codex_prompt_ready(self, session_name: str, timeout: float) -> bool:
        """Codex TUI readiness: wait for the input prompt without fallback Enter."""
        deadline = time.monotonic() + timeout
        trust_handled = False
        while time.monotonic() < deadline:
            try:
                pane = await capture_pane(session_name)
            except (OSError, subprocess.SubprocessError):
                return False
            if (
                not trust_handled
                and "Do you trust the contents of this directory?" in pane
                and "1. Yes, continue" in pane
            ):
                await asyncio.to_thread(
                    subprocess.run,
                    ["tmux", "send-keys", "-t", f"={session_name}:", "1", "Enter"],
                    capture_output=True,
                    check=False,
                )
                trust_handled = True
                await asyncio.sleep(0.5)
                continue
            if CODEX_ADAPTER.is_prompt_ready(pane):
                return True
            await asyncio.sleep(0.5)
        await asyncio.to_thread(
            subprocess.run, ["tmux", "kill-session", "-t", f"={session_name}"], capture_output=True
        )
        return False

    async def _probe_input_ready(
        self,
        session_name: str,
        *,
        get_input_bar_fn: Callable[[str], str | None],
    ) -> bool:
        """Verify TUI input handler is ready — send ONE probe char, then
        poll until it appears in the input bar (or timeout).

        Background: codex/claude render the prompt glyph (`>` / `>`)
        seconds before the input handler is wired up. Anything we paste
        before the handler is alive sits silently in the pty buffer
        until the TUI eventually drains it — sometimes 5-8 seconds
        later. Pasting MULTIPLE probe chars during that window leads
        to all of them landing at once when the TUI wakes up
        (regression observed 2026-04-26 23:36 UTC: `.тест` in input bar
        after probe-with-retries).

        The fix: paste exactly one probe character. Then poll the input
        bar every `_PROBE_INPUT_READY_RETRY_DELAY_SEC` seconds checking
        if the bar changed. When change is observed → probe landed →
        send ONE Backspace to wipe it → ready.

        If the change never comes within `_PROBE_INPUT_READY_BUDGET_SEC`
        (30s default), the TUI is presumed dead/stuck — return False so
        the caller kills the session.

        `get_input_bar_fn` is provider-specific: pass
        `codex_input_bar_content` for codex, `claude_input_bar_content`
        for claude. Logic is provider-agnostic.
        """
        try:
            pane_before = await capture_pane(session_name)
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "TUI_IO: probe capture-pane (baseline) failed session=%s",
                session_name,
            )
            return False
        bar_before = get_input_bar_fn(pane_before) or ""

        # One paste — never repeat. Repetition stacks dots in the pty
        # buffer that all land at once when the TUI wakes.
        try:
            await send_paste(session_name, _PROBE_CHAR)
        except (OSError, subprocess.SubprocessError):
            logger.warning("TUI_IO: probe send_paste failed session=%s", session_name)
            return False

        deadline = time.monotonic() + _PROBE_INPUT_READY_BUDGET_SEC
        polls = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(_PROBE_INPUT_READY_RETRY_DELAY_SEC)
            polls += 1
            try:
                pane_after = await capture_pane(session_name)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: probe capture-pane (poll) failed session=%s",
                    session_name,
                )
                # Best-effort cleanup: try to wipe the dot in case it
                # eventually lands. If capture is broken, send-keys may
                # be too — swallow errors.
                await self._cleanup_probe_residue(session_name, 1)
                return False
            bar_after = get_input_bar_fn(pane_after) or ""

            if bar_before != bar_after:
                # Probe landed. One paste -> one Backspace.
                await self._cleanup_probe_residue(session_name, 1)
                logger.info(
                    "TUI_IO: TUI ready (probe accepted) session=%s polls=%d",
                    session_name,
                    polls,
                )
                return True

        # Timeout: the dot never showed up. Try to wipe in case it
        # arrives late after kill.
        await self._cleanup_probe_residue(session_name, 1)

        logger.warning(
            "TUI_IO: TUI probe timeout session=%s budget=%.1fs polls=%d",
            session_name,
            _PROBE_INPUT_READY_BUDGET_SEC,
            polls,
        )
        return False

    async def _cleanup_probe_residue(self, session_name: str, probes_sent: int) -> None:
        """Send `Backspace` once per probe paste so accumulated dots are
        wiped before we hand the input bar back to the user.

        Why exactly `probes_sent` keypresses: each successful probe
        round appended one `_PROBE_CHAR` to the input bar (the
        placeholder eviction on the first round just replaces the hint
        with the dot - still one char). Sending Backspace x N restores
        the bar to its pre-probe shape. Failures are cosmetic - at
        worst the user sees a leading `.` in their next message; we
        log and move on rather than fail the readiness check on the
        cleanup side-effect.
        """
        if probes_sent <= 0:
            return
        try:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    session_name,
                    *(["BSpace"] * probes_sent),
                ],
                capture_output=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "TUI_IO: probe Backspace cleanup failed session=%s probes=%d",
                session_name,
                probes_sent,
            )

    def is_tailing(self, channel_key: ChannelKey) -> bool:
        """True if a tail loop is already running for this channel."""
        return channel_key in self._cancel_events

    async def _send_with_modal_guard(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        """Simplified Claude send pipeline: modal pre-check → paste → settle
        → modal recheck → Enter.

        Replaces the paste-verify retry loop (`_safe_send_and_enter` legacy
        path) for Claude sessions. The retry loop's `prompt_visible_in_pane`
        check produced false BLOCKED on every Claude TUI re-skin: 4 fixes in
        2 days on 2026-04-23..24, plus the 2026-05-07 incident with stacked
        paste residue from a prior BLOCKED. Empirical work in
        `work/reference/tgcc-server.js` (Oleg's bot) shows that paste
        verification is unnecessary when bracketed-paste delivery is used —
        a single settle pause between paste and Enter is sufficient.

        Defence remaining: `is_modal_present` is checked twice — before the
        paste and after — to ensure Enter never confirms a modal dialog.
        That is the load-bearing safety guarantee; the rest of the legacy
        verification was working around its own false positives.

        Contract:
          True  → Enter was sent.
          False → `_send_modal_alert` already posted a user-facing alert.

        Lock is taken by the caller (send_direct / send_stream); this method
        must not re-acquire it (would deadlock).
        """
        session_name = state.session_name
        logger.info(
            "TUI_IO: send_with_modal_guard session=%s len=%d",
            session_name,
            len(prompt),
        )

        # Step 1 — pre-send modal check. A modal here means Claude is
        # waiting on a dialog (model picker, /usage, trust, auto-compact);
        # pasting+Enter would silently confirm a menu item. An allowlisted
        # tool-permission dialog is answered first: the watchdog skips a
        # channel while its send lock is held, so without this a message
        # typed while CC asks about WebSearch turned into a manual alert
        # instead of the auto-approval the operator configured
        # (2026-10-28).
        pane_before = await capture_pane(session_name)
        tool = self._auto_approvable_tool(state, pane_before)
        if tool is not None:
            if await self._auto_approve_permission(channel_key, state, tool) == "failed":
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_before,
                    reason="claude_modal_before_send",
                )
                return False
            pane_before = await capture_pane(session_name)
        if is_modal_present(pane_before):
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_before,
                reason="claude_modal_before_send",
            )
            return False

        # Step 2 — bracketed paste, no Enter yet.
        # Step 3 — settle. Bracketed paste arrives as a single event but
        # the TUI needs a frame to absorb it; without this Enter races the
        # paste. Long/multiline payloads need more time to render the
        # `[Pasted text #N]` chip / wrapped lines.
        # Step 4 — post-paste modal check. Closes the race where a modal
        # popped between the pre-check and now (auto-compact prompt, MCP
        # auth, idle hooks). Cleanup with Ctrl+U so the orphaned paste
        # does not stack onto the next message's input bar — that was the
        # 2026-05-07 incident class. If that modal is an allowlisted
        # permission dialog it is approved and the paste repeated once —
        # the text landed while the dialog owned the keyboard and cannot
        # be trusted to sit in the input box.
        settle = (
            _GUARD_SETTLE_LONG_SEC
            if len(prompt) > _GUARD_LONG_PROMPT_THRESHOLD or "\n" in prompt
            else _GUARD_SETTLE_SHORT_SEC
        )
        pane_after_paste = pane_before
        for paste_attempt in range(2):
            try:
                await send_text_to_tmux(session_name, prompt, submit_enter=False)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: send_with_modal_guard send_text failed session=%s",
                    session_name,
                    exc_info=True,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_before,
                    reason="claude_send_error",
                )
                return False

            await asyncio.sleep(settle)

            pane_after_paste = await capture_pane(session_name)
            if not is_modal_present(pane_after_paste):
                break
            tool = self._auto_approvable_tool(state, pane_after_paste)
            approved = (
                paste_attempt == 0
                and tool is not None
                and await self._auto_approve_permission(channel_key, state, tool) != "failed"
            )
            try:
                await send_ctrl_u(session_name)
            except (OSError, subprocess.SubprocessError):
                # Best-effort — log and continue to alert. User must not
                # be left in silence even if cleanup itself fails.
                logger.warning(
                    "TUI_IO: send_with_modal_guard C-u cleanup failed session=%s",
                    session_name,
                    exc_info=True,
                )
            if approved:
                logger.info(
                    "TUI_IO: send_with_modal_guard re-paste after permission approval session=%s",
                    session_name,
                )
                continue
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after_paste,
                reason="claude_modal_after_send",
            )
            return False

        # Step 5 — Enter. No retry: a single Enter is sufficient on
        # bracketed-paste delivery (Oleg's bot uses one Enter; our Enter-
        # retry loop was working around its own false-detection of
        # "prompt didn't clear", not a real Enter delivery issue).
        try:
            await send_enter(session_name)
        except (OSError, subprocess.SubprocessError):
            logger.warning(
                "TUI_IO: send_with_modal_guard send_enter failed session=%s",
                session_name,
                exc_info=True,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after_paste,
                reason="claude_enter_send_error",
            )
            return False

        logger.info(
            "TUI_IO: send_with_modal_guard delivered session=%s settle=%.2fs",
            session_name,
            settle,
        )
        return True

    async def _safe_send_and_enter(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        """Capture → send-keys → verify-visible → pre-Enter re-capture → Enter.

        Returns True iff Enter was sent cleanly. On any failure path it
        posts a Telegram modal alert and returns False. Callers (send_direct
        and send_stream) must react to False — send_direct rolls back its
        command acknowledgement and live buffer; send_stream must not start the tail
        loop (CC would never emit a result_message, the tail would idle
        until cancel).

        `send-keys Enter` on a modal would confirm the selected dialog
        item — potentially approving a shell command or switching the
        model. That risk dominates all trade-offs here.
        """
        if state.provider == "codex":
            return await self._safe_send_codex(channel_key, state, prompt)

        return await self._send_with_modal_guard(channel_key, state, prompt)

    async def _safe_send_codex(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
    ) -> bool:
        """Codex-specific send policy: Enter-only.

        Algorithm (post-2026-04-26 simplification — Tab branches removed):

          1. capture pane_before, abort if a modal is already up.
          2. up to 3 paste attempts: send_text_to_tmux, poll for the
             prompt or `[Pasted Content]` chip to appear in the input
             bar; check for a modal between attempts.
          3. capture pane_pre_enter, abort if a modal raced in after
             the paste landed.
          4. up to 3 Enter attempts: send Enter, settle, capture; abort
             on modal_after_enter; succeed on input-bar cleared OR a
             queue marker that names this prompt.

        Every abort path posts a modal alert with a `reason` keyword that
        names the failure shape — those reasons grep cleanly in the
        TUI_ALERT_AUDIT log.

        Tab is *not* used here. Empirical 2026-04-26 22:13 UTC test on
        a busy codex confirmed Enter queues a follow-up identically to
        Tab. Tab's only old purpose — `[Pasted Content N chars]` chip
        expansion — is also obsolete now that bracketed paste (commit
        8d1363a4) collapses correctly on the first Enter.
        """
        session_name = state.session_name

        pane_before = await capture_pane(session_name)
        if CODEX_ADAPTER.is_modal_present(pane_before):
            logger.info(
                "TUI_IO: send BLOCKED session=%s len=%d reason=modal_before_send",
                session_name,
                len(prompt),
            )
            await self._send_modal_alert(
                channel_key, state, prompt, pane_before, reason="modal_before_send"
            )
            return False

        pane_after = pane_before
        delivered = False
        for paste_attempt in range(1, _CODEX_PASTE_RETRY_LIMIT + 1):
            # Double-check before re-pasting: when codex is mid-warmup
            # the FIRST paste may land AFTER the previous attempt's poll
            # deadline but BEFORE we get here. Issuing another paste in
            # that case duplicates the prompt in the input bar — exactly
            # the prod regression we saw 2026-04-26 23:36 UTC where the
            # user's message appeared 3-4 times stacked. Capture once
            # and check; only paste again if still not visible.
            if paste_attempt > 1:
                pane_recheck = await capture_pane(session_name)
                if CODEX_ADAPTER.is_modal_present(pane_recheck):
                    logger.info(
                        "TUI_IO: codex modal raced into pane between paste attempts "
                        "session=%s attempt=%d",
                        session_name,
                        paste_attempt,
                    )
                    await self._send_modal_alert(
                        channel_key,
                        state,
                        prompt,
                        pane_recheck,
                        reason="modal_during_paste",
                    )
                    return False
                if self._codex_delivery_visible(
                    pane_before, pane_recheck, prompt
                ) or self._codex_pasted_content_visible(pane_recheck):
                    logger.info(
                        "TUI_IO: codex previous paste landed late session=%s attempt=%d",
                        session_name,
                        paste_attempt,
                    )
                    pane_after = pane_recheck
                    delivered = True
                    break

            try:
                await send_text_to_tmux(session_name, prompt, submit_enter=False)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: codex send-paste failed session=%s attempt=%d",
                    session_name,
                    paste_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_before, reason="paste_send_error"
                )
                return False

            # Do-while semantics: at least one capture per attempt even
            # if budget is zero (unit tests patch budget to 0 to keep
            # them fast; we still need one observation to decide).
            deadline = time.monotonic() + _CODEX_PASTE_POLL_BUDGET_SEC
            modal_seen = False
            while True:
                await asyncio.sleep(_CODEX_PASTE_POLL_STEP_SEC)
                pane_after = await capture_pane(session_name)
                if CODEX_ADAPTER.is_modal_present(pane_after):
                    modal_seen = True
                    break
                if self._codex_delivery_visible(
                    pane_before, pane_after, prompt
                ) or self._codex_pasted_content_visible(pane_after):
                    delivered = True
                    break
                if time.monotonic() >= deadline:
                    break

            if modal_seen:
                logger.info(
                    "TUI_IO: send BLOCKED session=%s reason=modal_during_paste attempt=%d",
                    session_name,
                    paste_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_after, reason="modal_during_paste"
                )
                return False
            if delivered:
                break

            logger.info(
                "TUI_IO: codex paste not yet visible session=%s attempt=%d; retrying",
                session_name,
                paste_attempt,
            )

        if not delivered:
            pane_tail = "\n".join(
                (pane_after or pane_before).splitlines()[-_MODAL_DIAG_PANE_TAIL_LINES:]
            )
            logger.warning(
                "TUI_IO: codex paste not landed session=%s attempts=%d pane_tail=\n%s",
                session_name,
                _CODEX_PASTE_RETRY_LIMIT,
                pane_tail,
            )
            await self._send_modal_alert(
                channel_key,
                state,
                prompt,
                pane_after or pane_before,
                reason="paste_not_landed_after_retries",
            )
            return False

        pane_pre_enter = await capture_pane(session_name)
        if CODEX_ADAPTER.is_modal_present(pane_pre_enter):
            logger.info(
                "TUI_IO: send RACE session=%s reason=modal_after_paste",
                session_name,
            )
            await self._send_modal_alert(
                channel_key, state, prompt, pane_pre_enter, reason="modal_after_paste"
            )
            return False

        pane_current = pane_pre_enter
        for enter_attempt in range(1, _ENTER_RETRY_LIMIT + 1):
            try:
                await send_enter(session_name)
            except (OSError, subprocess.SubprocessError):
                logger.warning(
                    "TUI_IO: codex send Enter failed session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key, state, prompt, pane_current, reason="enter_send_error"
                )
                return False

            await asyncio.sleep(_ENTER_RETRY_SETTLE_SEC)
            pane_after_enter = await capture_pane(session_name)
            if not pane_after_enter:
                logger.warning(
                    "TUI_IO: codex Enter capture empty session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_current,
                    reason="enter_capture_empty",
                )
                return False

            if CODEX_ADAPTER.is_modal_present(pane_after_enter):
                # A modal that surfaced AFTER our Enter is still cause to
                # alert: we cannot tell from the pane alone whether the
                # modal pre-existed (Enter just confirmed it) or codex
                # raised it as a response. The /model dialog is the
                # cautionary tale — silently confirming settings is far
                # worse than asking the user to re-send a message.
                logger.info(
                    "TUI_IO: codex Enter opened modal session=%s attempt=%d",
                    session_name,
                    enter_attempt,
                )
                await self._send_modal_alert(
                    channel_key,
                    state,
                    prompt,
                    pane_after_enter,
                    reason="modal_after_enter",
                )
                return False

            # Codex success signal: the input bar no longer carries our
            # prompt. Both delivery shapes — direct submit on idle and
            # queue-on-busy — drop the prompt out of the codex input bar
            # (the queue ack `Messages to be submitted ...` doesn't render in
            # the input bar). Do NOT short-circuit on a queue marker
            # alone: a stale queue from a previous turn can sit in the
            # pane while our prompt still occupies the input bar — that
            # is a *retry* state, not success. The 2026-04-26
            # `test_codex_send_direct_rejects_stale_pending_queue_marker`
            # regression pins this.
            if not self._codex_prompt_still_in_input_bar(pane_after_enter, prompt):
                if enter_attempt > 1:
                    logger.info(
                        "TUI_IO: codex Enter accepted after retry session=%s attempt=%d",
                        session_name,
                        enter_attempt,
                    )
                else:
                    logger.info(
                        "TUI_IO: codex Enter accepted session=%s",
                        session_name,
                    )
                return True

            if enter_attempt < _ENTER_RETRY_LIMIT:
                logger.info(
                    "TUI_IO: codex Enter did not clear input session=%s attempt=%d; retrying",
                    session_name,
                    enter_attempt,
                )
            pane_current = pane_after_enter

        logger.warning(
            "TUI_IO: codex Enter exhausted retries session=%s attempts=%d",
            session_name,
            _ENTER_RETRY_LIMIT,
        )
        await self._send_modal_alert(
            channel_key,
            state,
            prompt,
            pane_current,
            reason="enter_did_not_clear_after_retries",
        )
        return False

    @staticmethod
    def _codex_prompt_visible(pane: str, prompt: str) -> bool:
        if not prompt.strip():
            return False

        # Codex wraps long pasted input in the visible TUI pane. A token like
        # "open-source" can render as "open-\nsource", which becomes
        # "open- source" after whitespace collapse. Match against the live
        # pane tail with hyphen-wraps normalized back to a single token.
        lines = pane.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        pane_tail = "\n".join(lines[-80:])
        if prompt in pane_tail:
            return True

        normalized = " ".join(prompt.split())
        pane_normalized = " ".join(pane_tail.split())
        pane_hyphen_compact = re.sub(r"-\s+", "-", pane_normalized)

        candidates = [normalized]
        if len(normalized) > 48:
            candidates.append(normalized[:48])
        if len(normalized) > 32:
            candidates.append(normalized[-32:])

        return any(
            candidate and (candidate in pane_normalized or candidate in pane_hyphen_compact)
            for candidate in candidates
        )

    def _codex_delivery_visible(self, pane_before: str, pane_after: str, prompt: str) -> bool:
        if codex_prompt_visible_in_pane(pane_before, pane_after, prompt):
            return True
        if CODEX_ADAPTER.is_modal_present(pane_after):
            return False
        return self._codex_prompt_visible(pane_after, prompt) and not self._codex_prompt_visible(
            pane_before, prompt
        )

    @staticmethod
    def _codex_pasted_content_visible(pane: str) -> bool:
        return "[pasted content" in pane.casefold()

    @staticmethod
    def _codex_prompt_still_in_input_bar(pane: str, prompt: str) -> bool:
        bar = codex_input_bar_content(pane)
        if not bar:
            return False
        if "[pasted content" in bar.casefold():
            return True

        normalized = " ".join(prompt.split())
        bar_normalized = " ".join(bar.split())
        bar_hyphen_compact = re.sub(r"-\s+", "-", bar_normalized)
        candidates = [normalized]
        if len(normalized) > 48:
            candidates.append(normalized[:48])
        if len(normalized) > 32:
            candidates.append(normalized[-32:])
        return any(
            candidate and (candidate in bar_normalized or candidate in bar_hyphen_compact)
            for candidate in candidates
        )

    async def send_direct(
        self,
        channel_key: ChannelKey,
        prompt: str,
        *,
        set_processing: bool = True,
    ) -> bool:
        """Deliver `prompt` to the tmux TUI, verifying the input actually
        landed before sending Enter. Returns True iff Enter was sent.

        Return value drives caller cleanup: on False, the caller must
        delete any command acknowledgement and close any LiveStatusBuffer
        it spawned — otherwise they hang forever with no signal back to
        the user.

        `set_processing=False` is for control commands (an in-TUI `/model`
        switch) that are not a processing turn: leaving the flag set would
        wedge the busy-checks until /cancel because no result event follows.
        """
        async with self._get_channel_lock(channel_key):
            state = self._sessions.get(channel_key)
            if not state:
                return False
            return await self._send_direct_locked(
                channel_key, state, prompt, set_processing=set_processing
            )

    async def _send_direct_locked(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
        *,
        set_processing: bool = True,
    ) -> bool:
        session_name = state.session_name
        logger.info("TUI_IO: send_direct session=%s len=%d", session_name, len(prompt))

        # Reuses the shared safe-send helper; on False the modal alert was
        # already posted. Setting the processing flag happens AFTER verify
        # so that a modal-blocked send doesn't leave `/uimode` picker busy.
        if not await self._safe_send_and_enter(channel_key, state, prompt):
            return False

        # Enter was delivered — flag must be set for the tail loop / busy
        # checks; send_stream's finally block clears it. For the pure
        # send_direct path (no tail loop follows), the tail-runner's
        # result_message will clear it, or cancel() will on timeout.
        if set_processing:
            self._is_processing[channel_key] = True
        logger.info("TUI_IO: send_direct delivered session=%s", session_name)
        return True

    async def _post_engine_loading_message(
        self,
        channel_key: ChannelKey,
        provider: str,
    ) -> object | None:
        """Post a "🔄 {engine} starting up" status message to the user's
        chat at the start of `start_session`. Returns the sent Message
        object so the caller can edit it on completion, or None if the
        bot is unwired (test scaffolding) or the post itself fails.

        Best-effort: any aiogram error is swallowed — failing to post a
        loading hint must not block the actual session spawn.
        """
        bot = self._bot
        if bot is None:
            return None
        try:
            message: object = await bot.send_message(  # type: ignore[attr-defined]
                chat_id=channel_key[0],
                text=t("ui.engine_starting", engine=provider),
                message_thread_id=channel_key[1],
            )
            return message
        except Exception:  # UX hint, never fatal
            logger.warning(
                "TUI_IO: failed to post engine loading message channel=%s",
                channel_key,
                exc_info=True,
            )
            return None

    async def _edit_engine_loading_message(
        self,
        sent: object | None,
        new_text: str,
    ) -> None:
        """Edit a previously posted loading message to its terminal
        state ("ready" or "failed"). No-op if `sent` is None
        (`_post_engine_loading_message` returned None) or if the edit
        fails (Telegram drop, message deleted by user, etc.)."""
        if sent is None:
            return
        edit = getattr(sent, "edit_text", None)
        if edit is None:
            return
        try:
            await edit(new_text)
        except Exception:  # UX hint, never fatal
            logger.warning(
                "TUI_IO: failed to edit engine loading message",
                exc_info=True,
            )

    async def _send_modal_alert(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        prompt: str,
        pane: str,
        *,
        reason: str = "unspecified",
    ) -> None:
        """Thin wrapper — impl in `tmux_modal_watchdog.send_modal_alert`.

        Kept on the class so tests can `patch.object(mgr, "_send_modal_alert", ...)`.
        `reason` names the failure path that triggered the alert and gets
        logged into TUI_ALERT_AUDIT for grep-based triage.
        """
        await _send_modal_alert_impl(self, channel_key, state, prompt, pane, reason=reason)

    async def _send_modal_idle_alert(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        pane: str,
        *,
        reason: str = "modal_idle_detected",
    ) -> None:
        """Thin wrapper — impl in `tmux_modal_watchdog.send_modal_idle_alert`."""
        await _send_modal_idle_alert_impl(self, channel_key, state, pane, reason=reason)

    # --- Modal watchdog ---

    def start_modal_watchdog(self, interval_sec: float = _MODAL_WATCHDOG_INTERVAL_SEC) -> None:
        """Launch the background task that scans every active session for
        idle modals. Idempotent — calling while a task is running is a
        no-op. Skipped if live-buffer wiring is absent (tests / live-mode
        disabled) because there's no Bot to post alerts with anyway."""
        if self._bot is None:
            logger.info("TUI_IO: modal watchdog skipped — no bot wired")
            return
        self._modal_watchdog.start(interval_sec)

    async def _modal_watchdog_loop(self, interval_sec: float) -> None:
        """Legacy in-class loop, delegates to ModalWatchdog.

        Kept for tests that directly instantiate the loop without going
        through start_modal_watchdog. New code should call `start_modal_watchdog`.
        """
        await self._modal_watchdog._loop(interval_sec)

    async def _check_channel_modal(self, channel_key: ChannelKey) -> None:
        """Single watchdog probe for one channel: skip when a send_direct
        is in flight, capture the pane, de-dup against the last alerted
        snapshot, detect, and post an idle-alert on a hit. If the pane no
        longer shows a modal, clear the dedup entry so a future modal
        (even with an identical pane hash by coincidence) will fire again.

        Tool-permission dialogs for operator-allowlisted tools (e.g.
        WebSearch under a managed CC build — see tui/permission_prompt.py)
        are answered with "1. Yes" instead of alerting; only a failed or
        ambiguous approval falls through to the manual alert."""
        lock = self._channel_locks.get(channel_key)
        if lock is not None and lock.locked():
            return
        state = self._sessions.get(channel_key)
        if state is None:
            return
        pane = await capture_pane(state.session_name)
        if not pane:
            return
        modal_present = (
            CODEX_ADAPTER.is_modal_present(pane)
            if state.provider == "codex"
            else is_modal_present(pane)
        )
        tool = self._auto_approvable_tool(state, pane)
        if tool is not None:
            if self._last_modal_pane.get(channel_key) == pane:
                return  # already handled this exact dialog snapshot
            outcome = await self._auto_approve_permission(channel_key, state, tool)
            if outcome == "approved":
                # Dedup on the pre-approval snapshot so the same dialog
                # can't trigger twice while the pane fades.
                self._last_modal_pane[channel_key] = pane
                return
            if outcome == "gone":
                self._last_modal_pane.pop(channel_key, None)
                return
            # "failed" — the dialog is still on screen. Alert below even
            # if the footer-token detector doesn't know this dialog class.
            modal_present = True
        if modal_present:
            if self._last_modal_pane.get(channel_key) == pane:
                return
            await self._send_modal_idle_alert(channel_key, state, pane)
        else:
            self._last_modal_pane.pop(channel_key, None)

    def _auto_approvable_tool(self, state: TmuxSessionState, pane: str) -> str | None:
        """Normalized tool name when the pane shows a structurally verified
        CC permission dialog for an operator-allowlisted tool, else None.
        Claude-only: codex/nessy approval dialogs have their own wording and
        stay on the manual path."""
        if not self._auto_approve_tools or state.provider != "claude":
            return None
        tool = parse_permission_prompt(pane)
        if tool is None or tool not in self._auto_approve_tools:
            return None
        return tool

    async def _auto_approve_permission(
        self, channel_key: ChannelKey, state: TmuxSessionState, tool: str
    ) -> Literal["approved", "gone", "failed"]:
        """Answer "1. Yes" on a verified CC tool-permission dialog.

        Re-captures the pane immediately before Enter — the watchdog tick's
        snapshot can be seconds old, and an Enter into a different modal is
        exactly the failure mode modal_detect.py exists to prevent. After
        each Enter, re-captures to confirm the dialog dismissed; a still
        live same-tool dialog is retried (a capture can catch the dialog
        mid-render, and an Enter pressed before the footer paints is
        swallowed). On success posts a chat notice so every bot-made
        approval is visible.
        """
        verified = await capture_pane(state.session_name)
        verified_tool = parse_permission_prompt(verified)
        if verified_tool is None:
            return "gone"  # resolved between ticks — nothing to do
        if verified_tool != tool:
            logger.warning(
                "TUI_IO: permission auto-approve skipped session=%s tool=%s fresh_tool=%s",
                state.session_name,
                tool,
                verified_tool,
            )
            return "failed"  # dialog changed under us — let the human look
        for attempt in range(1, _PERMISSION_ENTER_ATTEMPTS + 1):
            try:
                await send_enter(state.session_name)
            except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
                logger.warning(
                    "TUI_IO: permission auto-approve Enter failed session=%s exc=%s",
                    state.session_name,
                    type(exc).__name__,
                )
                return "failed"
            await asyncio.sleep(_PERMISSION_RESOLVE_WAIT_SEC)
            after = await capture_pane(state.session_name)
            after_tool = parse_permission_prompt(after)
            if after_tool is None:
                # Dialog gone — approved. A resolved dialog's scrollback
                # ghost parses as None, so this can't be a fade artifact.
                await _send_permission_notice_impl(
                    self, channel_key, state.session_name, tool, verified
                )
                return "approved"
            if after_tool != tool:
                logger.warning(
                    "TUI_IO: permission auto-approve switched session=%s tool=%s after_tool=%s",
                    state.session_name,
                    tool,
                    after_tool,
                )
                return "failed"  # different dialog now — let the human look
            # Same-tool dialog still live: Enter was lost (mid-render
            # capture), or the next same-tool dialog was issued instantly;
            # pressing Enter again is the right move in both cases.
            logger.info(
                "TUI_IO: permission auto-approve retry session=%s tool=%s attempt=%d",
                state.session_name,
                tool,
                attempt,
            )
        logger.warning(
            "TUI_IO: permission auto-approve did not dismiss session=%s tool=%s attempts=%d",
            state.session_name,
            tool,
            _PERMISSION_ENTER_ATTEMPTS,
        )
        return "failed"

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str:
        """Send user message to persistent CC TUI and tail transcript until done.

        Starts a long-running tail that streams all CC events via on_event.
        Returns empty string (results are sent immediately via result_message,
        not accumulated).

        Wave 3 B1: routes the prompt through `_safe_send_and_enter` so a
        modal-blocked session no longer blind-Enters on a dialog. On
        False, the tail is never started (CC would not emit a result
        event) and the finally block cleans up flags.
        """
        cancel_event = asyncio.Event()
        state: TmuxSessionState | None = None
        output_path: Path | None = None

        async def _on_event_with_processing(event: StreamEvent) -> None:
            if event.type == "result":
                # Bare sentinel — CC finished the turn (for the TUI
                # transcript it is synthesized on stop_reason=end_turn; see
                # tui/transcript.py). Clear the flag, then forward: the UI
                # posts its turn-done completion signal on this event.
                self._is_processing[channel_key] = False
                ret = on_event(event)
                if asyncio.iscoroutine(ret):
                    await ret
                return
            ret = on_event(event)
            if asyncio.iscoroutine(ret):
                await ret
            if event.type == "result_message":
                # Clear after Telegram send completes — the next prompt must
                # not be treated as idle-dispatched while the result is still
                # mid-post.
                self._is_processing[channel_key] = False

        try:
            async with self._get_channel_lock(channel_key):
                state = self._sessions[channel_key]
                self._is_processing[channel_key] = True
                logger.info(
                    "TUI_IO: send_stream session=%s len=%d",
                    state.session_name,
                    len(prompt),
                )

                # Cancel any running recovery/previous tail before starting a new one.
                existing = self._cancel_events.get(channel_key)
                if existing:
                    existing.set()
                    await self._wait_for_tail_exit(channel_key, timeout=0.5)

                self._cancel_events[channel_key] = cancel_event
                delivered = await self._safe_send_and_enter(channel_key, state, prompt)
                if not delivered:
                    # _safe_send_and_enter posted an alert; do not start tail.
                    return ""
                # New user turn — fresh auto-retry budget for transient API
                # errors, mirroring the manual "продолжай" the user could
                # always type again on the next message.
                self._api_error_retry.note_progress(channel_key)

                adapter = get_adapter(state.provider)
                if adapter.generates_own_session_id() and not state.transcript_path:
                    try:
                        await self._locate_transcript_after_send(channel_key, state, adapter)
                    except RuntimeError:
                        logger.warning(
                            "%s TUI transcript discovery failed after delivery; "
                            "leaving session alive channel=%s session=%s",
                            engine_display_name(state.provider),
                            channel_key,
                            state.session_name,
                            exc_info=True,
                        )
                        verb = "принял" if state.provider == "codex" else "приняла"
                        engine = engine_display_name(state.provider)
                        ret = on_event(
                            StreamEvent(
                                "result_message",
                                f"{engine} {verb} сообщение, но бот не смог найти transcript "
                                "для стриминга ответа. Сессия оставлена живой; открой /tui "
                                "или отправь следующее сообщение после завершения работы.",
                            )
                        )
                        if asyncio.iscoroutine(ret):
                            await ret
                        await self.close_buffer(channel_key)
                        return ""

                output_path = self._transcript_path_for_state(state)
            if output_path is None:
                target = state.session_name if state else channel_key
                logger.warning("No transcript path for %s", target)
                return ""
            _result_text, _new_session_id = await self._tail_until_done(
                output_path, state, _on_event_with_processing, cancel_event
            )

            # state.offset is maintained incrementally by _tail_until_done
            # from the bytes it actually consumed. Re-reading file_size
            # here would leap over any events written between tail's last
            # read and this point (e.g. after cancel was signalled),
            # silently dropping them on the next resume.
            self._save_state()

            # Results are sent immediately via result_message events.
            # Returning the text would cause streaming.py to send it again
            # as the "final" message, producing a duplicate.
            return ""
        finally:
            if self._cancel_events.get(channel_key) is cancel_event:
                self._cancel_events.pop(channel_key, None)
            self._is_processing.pop(channel_key, None)

    async def cancel(self, channel_key: ChannelKey) -> None:
        """Interrupt CC processing and cancel the tail loop.

        Sends Escape via `tmux send-keys` to interrupt the current CC
        operation, then unblocks the tail.
        """
        state = self._sessions.get(channel_key)
        if state:
            logger.info("TUI_IO: cancel session=%s", state.session_name)
            subprocess.run(
                ["tmux", "send-keys", "-t", f"={state.session_name}:", "Escape"],
                capture_output=True,
            )

        # Close buffer BEFORE signalling cancel: once the tail sees the event
        # it may return from _tail_until_done and leave the buffer's worker
        # orphaned.
        await self.close_buffer(channel_key)

        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            logger.info("Cancelled tmux tail for channel %s", channel_key)

        # Clear the processing flag unconditionally. Recovery tails and
        # send_direct callers never install _on_event_with_processing, so
        # without this clear the flag would stay True forever after /kill
        # or /cancel — and any subsequent busy-check would be stuck.
        self._is_processing.pop(channel_key, None)

        # Offset is maintained incrementally by _tail_until_done — no
        # post-cancel recompute needed.

    async def reconnect_tail(
        self, channel_key: ChannelKey
    ) -> Literal["started", "no_session", "no_transcript", "needs_first_message"]:
        """Cancel any stuck tail and restart streaming from the current offset.

        Does NOT touch the tmux session or Claude's context — only restarts
        the Python-side transcript reader.

        Returns:
          "started"             — tail restarted successfully.
          "no_session"          — no active tmux session for this channel.
          "needs_first_message" — Nessy/Codex session alive but no transcript
                                  yet; user must send the first prompt.
          "no_transcript"       — session alive, transcript path known but
                                  the file is missing (rare; suggest /clear).
        """
        state = self._sessions.get(channel_key)
        if state is None:
            return "no_session"

        # Cancel stuck tail without sending Escape to Claude.
        await self.close_buffer(channel_key)
        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
        self._is_processing.pop(channel_key, None)
        await self._wait_for_tail_exit(channel_key, timeout=1.0)

        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            if state.provider in {"nessy", "codex"} and not state.session_id:
                return "needs_first_message"
            return "no_transcript"
        if not output_path.exists():
            return "no_transcript"

        started = await self._start_recovery_tail(
            channel_key, state, output_path, reason="reconnect"
        )
        if started:
            logger.info(
                "TUI_IO: reconnect_tail started channel=%s session=%s offset=%d",
                channel_key,
                state.session_name,
                state.offset,
            )
        return "started" if started else "no_transcript"

    async def _wait_for_tail_exit(self, channel_key: ChannelKey, timeout: float = 2.5) -> None:
        """Busy-wait up to `timeout` seconds for the tail's finally block
        to pop its cancel_event from `_cancel_events`.

        Shared helper used by `clear_context` and `switch_session` — both
        mutate tmux after cancelling any in-flight tail, and must let the
        tail's finally block clean up first so the next `_tail_until_done`
        doesn't see a stale cancel_event.
        """
        iterations = max(1, int(timeout / 0.05))
        for _ in range(iterations):
            if channel_key not in self._cancel_events:
                return
            await asyncio.sleep(0.05)
        logger.warning("Timed out waiting for tail cleanup for %s", channel_key)

    async def clear_context(self, channel_key: ChannelKey, session_manager: object) -> bool:
        """Reset CC context by respawning tmux with a fresh `--session-id`.

        Historical note: earlier versions sent `Escape → /clear → Enter` into
        the live CC TUI. That leaked session-id drift — CC TUI silently
        rotates its internal session_id on `/clear` and writes subsequent
        events to a new transcript jsonl, while the bot kept tailing the old
        (dead) file. Respawn keeps the UUID under the bot's control.

        Returns True if a live tmux session was found and respawned,
        False if tmux was already dead.
        """
        state = self._sessions.get(channel_key)
        if not state or not self._tmux_alive(state.session_name):
            return False

        logger.info("TUI_IO: clear_context session=%s", state.session_name)

        # Close buffer before cancelling the tail — see cancel() for rationale.
        await self.close_buffer(channel_key)

        # Unblock any active tail loop, then wait for its finally block to
        # clear the cancel_event.
        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            await self._wait_for_tail_exit(channel_key)

        state.mcp_config = self._ensure_runtime_mcp_config(
            channel_key=channel_key,
            base_mcp_config=state.base_mcp_config or state.mcp_config,
            session_dir=Path(state.session_dir),
            session_manager=session_manager,
        )
        if state.provider == "codex":
            existing = set(Path.home().joinpath(".codex", "sessions").glob("**/*.jsonl"))
            since_wall = time.time()
            new_session_id = None
            startup_cmd = CODEX_ADAPTER.build_tui_start(
                cwd=state.cwd,
                model=state.model,
                mcp_config=state.mcp_config,
            )
            state.session_id = None
            state.transcript_path = None
        elif state.provider == "nessy":
            new_session_id = None
            startup_cmd = NESSY_ADAPTER.build_tui_start(
                cwd=state.cwd,
                model=state.model,
                mcp_config=state.mcp_config,
            )
            state.session_id = None
            state.transcript_path = None
        else:
            new_session_id = generate_session_uuid()
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=state.mode,
                mcp_config=state.mcp_config,
                session_id_new=new_session_id,
                model=state.model,
            )
            state.session_id = new_session_id
            state.transcript_path = None

        state.offset = 0
        self._save_state()
        try:
            await self._spawn_tmux(
                name=state.session_name,
                session_dir=Path(state.session_dir),
                cwd=state.cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=state.provider,
            )
        except RuntimeError:
            # Spawn failed — zero the offset so a future tail doesn't seek
            # into a missing file with a stale position, then propagate.
            logger.warning(
                "clear_context respawn failed for %s; persisted reset session_id=%s",
                state.session_name,
                state.session_id,
            )
            state.offset = 0
            self._save_state()
            raise
        adapter = get_adapter(state.provider)
        if adapter.generates_own_session_id():
            if state.provider == "nessy":
                self._tui_start_snapshots[channel_key] = NESSY_ADAPTER.transcript_snapshot()
            else:
                self._tui_start_snapshots[channel_key] = (existing, since_wall)
        logger.info(
            "Respawned tmux session %s with fresh session %s",
            state.session_name,
            new_session_id,
        )
        return True

    async def switch_session(
        self,
        channel_key: ChannelKey,
        new_session_id: str,
        session_manager: object,
    ) -> bool:
        """Switch the tmux CC to a different session.

        Guard (Decision 13): if the target transcript file does not exist
        on disk, refuse to touch tmux/state and return False.

        Returns:
          True  — kill+spawn completed, `state.session_id == new_session_id`.
          False — target transcript missing; tmux/state unchanged.
        """
        state = self._sessions.get(channel_key)
        if not state:
            return False

        # Shape-check the incoming uuid BEFORE handing it to transcript_path —
        # the helper's UUID4 assert would otherwise raise AssertionError for
        # legacy or malformed ids.
        if state.provider == "codex":
            target_transcript = self._find_codex_transcript(new_session_id, state.cwd)
        elif state.provider == "nessy":
            from telegram_bot.core.services.providers import NESSY_ADAPTER

            target_transcript = NESSY_ADAPTER.transcript_path_for_state(
                cwd=state.cwd,
                session_id=new_session_id,
                transcript_path=None,
            )
        else:
            from telegram_bot.core.tui.paths import _SESSION_ID_RE

            if not _SESSION_ID_RE.fullmatch(new_session_id):
                logger.info("switch_session: malformed target session_id %r", new_session_id)
                return False
            target_transcript = transcript_path(state.cwd, new_session_id)
        if target_transcript is None:
            logger.info("switch_session: target transcript missing for %s", new_session_id)
            return False
        if not target_transcript.exists():
            logger.info("switch_session: target transcript missing for %s", new_session_id)
            return False

        # Close the buffer first so the old session's thinking page stops
        # accepting appends and has its keyboard stripped.
        await self.close_buffer(channel_key)

        event = self._cancel_events.get(channel_key)
        if event:
            event.set()
            await self._wait_for_tail_exit(channel_key)

        # Resume an existing transcript — guard above confirmed target
        # jsonl exists. `--session-id` would make CC bail with "Session
        # ID is already in use".
        state.mcp_config = self._ensure_runtime_mcp_config(
            channel_key=channel_key,
            base_mcp_config=state.base_mcp_config or state.mcp_config,
            session_dir=Path(state.session_dir),
            session_manager=session_manager,
        )
        if state.provider == "codex":
            startup_cmd = CODEX_ADAPTER.build_tui_resume(
                cwd=state.cwd,
                session_id=new_session_id,
                model=state.model,
                mcp_config=state.mcp_config,
            )
        elif state.provider == "nessy":
            startup_cmd = NESSY_ADAPTER.build_tui_resume(
                cwd=state.cwd,
                session_id=new_session_id,
                model=state.model,
                mcp_config=state.mcp_config,
            )
        else:
            startup_cmd = session_manager.build_tmux_startup_args(  # type: ignore[attr-defined]
                mode=state.mode,
                mcp_config=state.mcp_config,
                resume_session_id=new_session_id,
                model=state.model,
            )
        try:
            await self._spawn_tmux(
                name=state.session_name,
                session_dir=Path(state.session_dir),
                cwd=state.cwd,
                startup_cmd=startup_cmd,
                channel_key=channel_key,
                provider=state.provider,
            )
        except RuntimeError:
            state.offset = 0
            self._save_state()
            raise
        state.session_id = new_session_id
        state.transcript_path = (
            str(target_transcript) if state.provider in ("codex", "nessy") else None
        )
        # Seed past all events already in the target transcript — offset=0
        # would re-emit every historical event through on_event.
        state.offset = self._file_size(target_transcript)
        self._save_state()
        logger.info(
            "Switched tmux session %s to CC session %s (offset=%d)",
            state.session_name,
            new_session_id,
            state.offset,
        )
        return True

    async def switch_or_start_session(
        self,
        channel_key: ChannelKey,
        target_session_id: str,
        target_provider: Engine,
        target_transcript_path: Path,
        *,
        session_manager: object,
        topic_config: TopicConfig,
        defaults: BotDefaults,
    ) -> SwitchResult:
        """Switch live tmux to a selected transcript, or start it if dormant."""
        async with self._get_channel_lock(channel_key):
            settings = topic_config.get_topic(channel_key[1])
            runtime = resolve_topic_runtime_config(settings, defaults)

            if not self._validate_session_id_shape(target_session_id, target_provider):
                return SwitchResult(kind="invalid_id")
            if not target_transcript_path.exists():
                return SwitchResult(kind="target_missing")

            thread_id = channel_key[1]
            if thread_id is None:
                return SwitchResult(kind="config_write_failed")

            captured = self._sessions.get(channel_key)
            engine_changed = target_provider != runtime.engine
            mode_changed = runtime.exec_mode != "tmux"
            if mode_changed and engine_changed:
                ok = await topic_config.update_engine_model_exec_mode(
                    thread_id,
                    target_provider,
                    None,
                    "tmux",
                )
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                clear_provider = getattr(session_manager, "clear_provider_session", None)
                if clear_provider is not None:
                    await clear_provider(channel_key)
                runtime = replace(runtime, engine=target_provider, model=None, exec_mode="tmux")
            elif mode_changed:
                ok = await topic_config.update_exec_mode(thread_id, "tmux")
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                runtime = replace(runtime, exec_mode="tmux")
            elif engine_changed:
                ok = await topic_config.update_engine_model(thread_id, target_provider, None)
                if not ok:
                    return SwitchResult(kind="config_write_failed")
                clear_provider = getattr(session_manager, "clear_provider_session", None)
                if clear_provider is not None:
                    await clear_provider(channel_key)
                runtime = replace(runtime, engine=target_provider, model=None)

            if (
                captured
                and self._tmux_alive(captured.session_name)
                and captured.session_id == target_session_id
                and captured.provider == target_provider
            ):
                return SwitchResult(
                    kind="already_on_it",
                    engine_changed=engine_changed,
                    mode_changed=mode_changed,
                )

            if captured and self._tmux_alive(captured.session_name):
                await self.close_buffer(channel_key)
                cancel_event = self._cancel_events.get(channel_key)
                if cancel_event:
                    cancel_event.set()
                    await self._wait_for_tail_exit(channel_key)
                await self._kill_tmux_only(channel_key, captured)

            runtime_mcp_config = self._ensure_runtime_mcp_config(
                channel_key=channel_key,
                base_mcp_config=runtime.mcp_config,
                session_dir=self._sessions_dir / self._make_name(channel_key),
                session_manager=session_manager,
            )
            runtime_for_resume = replace(runtime, mcp_config=runtime_mcp_config)
            new_state = self._build_state_for_resume(
                channel_key=channel_key,
                runtime=runtime_for_resume,
                provider=target_provider,
                session_id=target_session_id,
                transcript_path=target_transcript_path,
            )
            new_state.base_mcp_config = runtime.mcp_config
            startup_cmd = build_resume_startup_cmd(
                target_provider,
                cwd=runtime.cwd,
                session_id=target_session_id,
                mode=runtime.mode,
                mcp_config=runtime_for_resume.mcp_config,
                model=runtime.model,
                session_manager=session_manager,
            )
            try:
                await self._spawn_tmux(
                    name=new_state.session_name,
                    session_dir=Path(new_state.session_dir),
                    cwd=str(runtime.cwd),
                    startup_cmd=startup_cmd,
                    channel_key=channel_key,
                    provider=target_provider,
                )
            except RuntimeError:
                self._sessions.pop(channel_key, None)
                self._save_state()
                return SwitchResult(
                    kind="spawn_failed",
                    engine_changed=engine_changed,
                    mode_changed=mode_changed,
                )

            new_state.offset = self._file_size(target_transcript_path)
            self._sessions[channel_key] = new_state
            self._save_state()
            return SwitchResult(
                kind="switched" if captured else "started",
                engine_changed=engine_changed,
                mode_changed=mode_changed,
                model=runtime.model,
            )

    async def kill(self, channel_key: ChannelKey) -> None:
        """Kill tmux session for the channel.

        Wave 3 3.5: additionally clears per-channel transient state
        (`_last_modal_pane`, `_channel_locks`, `_spawn_deadlines`, `_buffers`)
        so a subsequent `start_session` on the same channel starts from a
        clean slate. Without this, stale send-locks from a killed session
        could serialise unrelated work, and a stale `_last_modal_pane`
        entry could suppress a new session's modal alert.
        """
        async with self._get_channel_lock(channel_key):
            await self._kill_session_unlocked(channel_key)

    async def _kill_session_unlocked(self, channel_key: ChannelKey) -> None:
        # Cancel tail loop first so send_stream unblocks.
        await self.cancel(channel_key)

        state = self._sessions.pop(channel_key, None)
        if state:
            await self._kill_tmux_only(channel_key, state)
            self._save_state()
            logger.info("Killed tmux session %s", state.session_name)

        # Clear per-channel transient state except the lifecycle lock. Locks
        # are intentionally stable so waiters cannot split across old/new locks.
        self._last_modal_pane.pop(channel_key, None)
        self._tui_panel_messages.pop(channel_key, None)
        self._spawn_deadlines.pop(channel_key, None)
        self._tui_start_snapshots.pop(channel_key, None)

    async def _kill_tmux_only(self, channel_key: ChannelKey, state: TmuxSessionState) -> None:
        """Kill tmux process only; caller owns state lifecycle."""
        _ = channel_key
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "kill-session", "-t", f"={state.session_name}"],
            capture_output=True,
        )

    def restore_all(
        self, session_manager: object | None = None
    ) -> dict[ChannelKey, TmuxSessionState]:
        """Load persisted sessions with runner_version awareness.

        Delegates to `tmux_recovery.restore_all` — see that module for the
        full behavior matrix.
        """
        return _restore_all_impl(self, session_manager)

    async def resume_tails(
        self,
        on_event_factory: Callable[[ChannelKey], Callable[[StreamEvent], Awaitable[None] | None]],
    ) -> None:
        """Start recovery tails for all alive sessions on bot startup.

        Delegates to `tmux_recovery.resume_tails`.
        """
        self._recovery_on_event_factory = on_event_factory
        await _resume_tails_impl(self, on_event_factory)

    async def ensure_recovery_tail(self, channel_key: ChannelKey) -> bool:
        """Start a recovery tail for manual TUI actions if no tail is active.

        `/tui` can unblock Codex/Claude by pressing Enter or selecting a modal
        option. Those outputs are not caused by `send_stream`, so without this
        hook the transcript is only drained on the next Telegram message.
        """
        if channel_key in self._cancel_events:
            return False
        state = self._sessions.get(channel_key)
        if state is None or not state.session_id:
            return False
        if not self._tmux_alive(state.session_name):
            return False
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            return False
        return await self._start_recovery_tail(channel_key, state, output_path, reason="manual")

    async def _start_recovery_tail(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        output_path: Path,
        *,
        reason: str = "watchdog",
    ) -> bool:
        if self._recovery_on_event_factory is None:
            return False

        on_event = self._recovery_on_event_factory(channel_key)
        cancel_event = asyncio.Event()
        self._cancel_events[channel_key] = cancel_event
        asyncio.create_task(  # noqa: RUF006
            _run_recovery_tail_impl(self, channel_key, state, output_path, on_event, cancel_event)
        )
        logger.info(
            "Started %s recovery tail for channel %s (offset=%d)",
            reason,
            channel_key,
            state.offset,
        )
        return True

    async def _run_recovery_tail(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        output_path: Path,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
        cancel_event: asyncio.Event,
    ) -> None:
        """Delegate to `tmux_recovery.run_recovery_tail`.

        Kept on the class for tests that call `mgr._run_recovery_tail(...)`
        directly (regression coverage for B2 offset preservation).
        """
        from telegram_bot.core.services.tmux_recovery import run_recovery_tail

        await run_recovery_tail(self, channel_key, state, output_path, on_event, cancel_event)

    # --- Activity watchdog ---

    def start_activity_watchdog(
        self, interval_sec: float = _ACTIVITY_WATCHDOG_INTERVAL_SEC
    ) -> None:
        """Launch the background task that streams tmux-typed turns to Telegram.

        Motivation (2026-10-26 bug): tails only exist while a Telegram-originated
        turn is running. Output produced by typing straight into the tmux pane
        accumulated unread, then flooded the topic as one backlog dump when the
        next Telegram message started a tail. The watchdog stats each session's
        transcript every `interval_sec`; growth beyond the delivered offset
        with no tail running means someone is driving the pane directly, so a
        recovery tail is started to drain it live. Idempotent.
        """
        if self._activity_watchdog_task is not None:
            return
        self._activity_watchdog_task = asyncio.create_task(
            self._activity_watchdog_loop(interval_sec)
        )

    async def _activity_watchdog_loop(self, interval_sec: float) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            for key in list(self._sessions.keys()):
                try:
                    await self._check_channel_activity(key)
                except Exception:
                    logger.warning("activity watchdog tick failed for %s", key, exc_info=True)

    async def _check_channel_activity(self, channel_key: ChannelKey) -> None:
        """Start a recovery tail iff the transcript grew while no tail is running."""
        if channel_key in self._cancel_events:
            return  # a tail is already draining this channel
        state = self._sessions.get(channel_key)
        if state is None or not self._tmux_alive(state.session_name):
            return
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            return
        try:
            size = output_path.stat().st_size
        except OSError:
            return
        if size <= state.offset:
            return
        await self._start_recovery_tail(channel_key, state, output_path, reason="activity")

    # --- Auto-compact watchdog ---

    def start_auto_compact_watchdog(
        self, *, default_tokens: int, interval_sec: float, continue_prompt: str = ""
    ) -> None:
        """Launch the background task that compacts oversized sessions.

        Every `interval_sec` each live Claude session's transcript is read
        for its current context size (`tui/context_usage.py`); once it
        exceeds the topic's threshold (`TopicSettings.auto_compact_tokens`,
        falling back to `default_tokens`), the engine's compact command is
        typed into the pane exactly as a user `/compact` would be and a
        notice is posted. `continue_prompt` follows so the task picks up
        after the compaction: typed right behind the compact on an idle
        session, or once the compact_boundary lands on a busy one (CC
        absorbs plain text into a running turn, see `_check_channel_context`).
        `default_tokens=0` with no topic overrides makes the loop a no-op.
        Idempotent.
        """
        self._auto_compact_default_tokens = max(0, default_tokens)
        self._auto_compact_continue_prompt = continue_prompt.strip()
        if self._auto_compact_task is not None:
            return
        self._auto_compact_task = asyncio.create_task(self._auto_compact_loop(interval_sec))

    async def _auto_compact_loop(self, interval_sec: float) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            for key in list(self._sessions.keys()):
                try:
                    await self._check_channel_context(key)
                except Exception:
                    logger.warning("auto-compact tick failed for %s", key, exc_info=True)

    def auto_compact_threshold(self, channel_key: ChannelKey) -> int:
        """Effective threshold for a channel: topic override, else the bot
        default. 0 means disabled."""
        thread_id = channel_key[1]
        get_topic = getattr(self._topic_config, "get_topic", None)
        override = None
        if thread_id is not None and get_topic is not None:
            override = getattr(get_topic(thread_id), "auto_compact_tokens", None)
        if isinstance(override, int) and not isinstance(override, bool):
            return max(0, override)
        return self._auto_compact_default_tokens

    def context_tokens(self, channel_key: ChannelKey) -> int | None:
        """Current context size of the channel's session, if measurable."""
        state = self._sessions.get(channel_key)
        if state is None or state.provider != "claude":
            return None
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            return None
        usage = read_context_usage(output_path)
        return usage.tokens if usage else None

    async def _check_channel_context(self, channel_key: ChannelKey) -> None:
        """Type the compact command iff the newest context measurement of a
        live Claude session exceeds the threshold and has not been acted on."""
        threshold = self.auto_compact_threshold(channel_key)
        if threshold <= 0:
            return
        state = self._sessions.get(channel_key)
        # Only Claude transcripts expose per-call usage; codex/nessy have
        # no comparable signal, so they keep manual /compact only.
        if state is None or state.provider != "claude":
            return
        if not self._tmux_alive(state.session_name):
            return
        output_path = self._transcript_path_for_state(state)
        if output_path is None:
            return
        if self._compact_still_pending(channel_key, output_path):
            return
        usage = read_context_usage(output_path)
        if usage is None or usage.tokens < threshold:
            self._auto_compact_seen.pop(channel_key, None)
            return
        if self._auto_compact_seen.get(channel_key) == usage.line_id:
            return  # already compacted on this measurement; waiting for a new turn
        pane = await capture_pane(state.session_name)
        # A compaction already running (transcript gate missed it, e.g. the
        # pane line is older than the scan window) still shows in the pane.
        if "compacting" in pane.lower():
            return
        # Busy-at-trigger decides when the continue prompt may go in: CC
        # absorbs ordinary text into a running turn (`absorbed_mid_turn` in
        # the transcript) but keeps slash commands queued until it ends, so
        # a continue typed now would be swallowed by the pre-compact turn
        # and the task would still stall after the compaction lands. Wait
        # for the compact_boundary in that case; an idle session has
        # nothing to absorb it and gets both entries back-to-back.
        busy = self.is_processing(channel_key) or "esc to interrupt" in pane.lower()
        # Mark before sending: a modal-blocked send already posts its own
        # alert, and retrying every tick would only spam it. The next
        # assistant turn produces a new line_id and re-arms the check.
        self._auto_compact_seen[channel_key] = usage.line_id
        command = get_adapter(state.provider).compact_slash_command()
        try:
            offset_before = output_path.stat().st_size
        except OSError:
            offset_before = 0
        logger.info(
            "TUI_IO: auto-compact session=%s tokens=%d threshold=%d busy=%s",
            state.session_name,
            usage.tokens,
            threshold,
            busy,
        )
        delivered = await self.send_direct(channel_key, command, set_processing=False)
        if not delivered:
            return
        self._auto_compact_pending[channel_key] = (
            offset_before,
            time.monotonic() + _AUTO_COMPACT_RESUME_DEADLINE_SEC,
        )
        await self._post_plain_notice(
            channel_key,
            state,
            t("ui.auto_compact_triggered", tokens=usage.tokens, threshold=threshold),
        )
        if not self._auto_compact_continue_prompt:
            return
        if busy:
            task = asyncio.create_task(
                self._resume_after_compact(channel_key, state, output_path, offset_before)
            )
            self._auto_compact_resume_tasks.add(task)
            task.add_done_callback(self._auto_compact_resume_tasks.discard)
            return
        # Idle: nothing to absorb the text, so the continue goes in right
        # behind the compact and CC runs it as soon as the summary lands.
        await asyncio.sleep(_AUTO_COMPACT_RESUME_SETTLE_SEC)
        if self._sessions.get(channel_key) is not state:
            return  # replaced by /new or /cwd during the pause
        resumed = await self.send_direct(channel_key, self._auto_compact_continue_prompt)
        if resumed:
            logger.info("TUI_IO: auto-compact continue queued session=%s", state.session_name)
            await self._post_plain_notice(channel_key, state, t("ui.auto_compact_resumed"))

    async def _resume_after_compact(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        output_path: Path,
        offset_before: int,
    ) -> None:
        """Send the continue prompt once the compaction typed into a busy
        session has finished.

        Completion is read from the transcript (a `compact_boundary` line
        appended after `offset_before`) rather than the pane: the pane
        shows "Compacting…" only briefly and the queued `/compact` may not
        even start until the current turn ends, which can take a long
        time — hence the generous deadline. Gives up silently when the
        session is replaced (`/new`, `/cwd`) or dies in the meantime.
        """
        deadline = time.monotonic() + _AUTO_COMPACT_RESUME_DEADLINE_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(_AUTO_COMPACT_RESUME_POLL_SEC)
            if self._sessions.get(channel_key) is not state:
                return
            if not self._tmux_alive(state.session_name):
                return
            if compact_boundary_since(output_path, offset_before):
                break
        else:
            logger.info(
                "TUI_IO: auto-compact resume gave up session=%s (no compact_boundary)",
                state.session_name,
            )
            return
        # Let CC redraw the prompt after the boundary is written.
        await asyncio.sleep(_AUTO_COMPACT_RESUME_SETTLE_SEC)
        if self._sessions.get(channel_key) is not state:
            return
        logger.info("TUI_IO: auto-compact resume session=%s", state.session_name)
        delivered = await self.send_direct(channel_key, self._auto_compact_continue_prompt)
        if delivered:
            await self._post_plain_notice(channel_key, state, t("ui.auto_compact_resumed"))

    def _compact_still_pending(self, channel_key: ChannelKey, output_path: Path) -> bool:
        """True while a `/compact` has neither produced its compact_boundary
        nor timed out — this process's own or one found in the transcript.

        The transcript side is what survives a bot restart and catches
        compactions typed by hand; the in-memory record only adds the byte
        offset that `_resume_after_compact` polls from. Clears the
        in-memory gate once its compaction lands or times out.
        """
        pending = self._auto_compact_pending.get(channel_key)
        if pending is not None:
            offset, deadline = pending
            if time.monotonic() >= deadline or compact_boundary_since(output_path, offset):
                del self._auto_compact_pending[channel_key]
            else:
                return True
        return compact_pending(output_path, now=time.time())

    async def _post_plain_notice(
        self, channel_key: ChannelKey, state: TmuxSessionState, text: str
    ) -> None:
        """Informational chat line from a background task — never fatal."""
        bot = self._bot
        if bot is None:
            return
        try:
            await bot.send_message(  # type: ignore[attr-defined]
                chat_id=channel_key[0],
                text=text,
                message_thread_id=channel_key[1],
            )
        except Exception:
            logger.warning(
                "TUI_IO: notice failed session=%s text=%r",
                state.session_name,
                text[:60],
                exc_info=True,
            )

    def _spawn_tmux_sync(
        self,
        *,
        name: str,
        session_dir: Path,
        cwd: str,
        startup_cmd: list[str],
    ) -> bool:
        """Synchronous respawn used by `restore_all`. Kept on the class so
        subclasses / tests can patch it. Delegates to `tmux_spawn.spawn_tmux_sync`.
        """
        return spawn_tmux_sync(
            name=name,
            session_dir=session_dir,
            cwd=cwd,
            startup_cmd=startup_cmd,
        )

    # --- Internal helpers ---

    def _topic_name(self, channel_key: ChannelKey | None) -> str | None:
        """Telegram topic title for the tmux window label, if known."""
        thread_id = channel_key[1] if channel_key else None
        get_topic = getattr(self._topic_config, "get_topic", None)
        if thread_id is None or get_topic is None:
            return None
        name = getattr(get_topic(thread_id), "name", None)
        return name if isinstance(name, str) else None

    def _make_name(self, channel_key: ChannelKey, provider: str = "claude") -> str:
        """Channel-key → tmux session name, using provider-specific prefix."""
        prefix = get_adapter(provider).session_name_prefix()
        return make_session_name(channel_key, prefix=prefix)

    def _ensure_runtime_mcp_config(
        self,
        *,
        channel_key: ChannelKey,
        base_mcp_config: str | None,
        session_dir: Path,
        session_manager: object | None = None,
    ) -> str:
        project_root = None
        default_mcp = ""
        if session_manager is not None:
            settings = getattr(session_manager, "_settings", None)
            project_root = getattr(settings, "project_root", None)
            default_getter = getattr(session_manager, "default_mcp_config_path", None)
            if callable(default_getter):
                default_mcp = str(default_getter())
        return ensure_bot_runtime_mcp_config(
            base_mcp_config=base_mcp_config or default_mcp or None,
            channel_key=channel_key,
            runtime_path=session_dir / "mcp.runtime.json",
            project_root=project_root,
        )

    def _get_channel_lock(self, channel_key: ChannelKey) -> asyncio.Lock:
        """Return the stable per-channel lifecycle lock. Never delete entries."""
        lock = self._channel_locks.get(channel_key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_key] = lock
        return lock

    @staticmethod
    def _validate_session_id_shape(session_id: str, provider: str) -> bool:
        return get_adapter(provider).validate_session_id(session_id)

    def _build_state_for_resume(
        self,
        *,
        channel_key: ChannelKey,
        runtime: TopicRuntimeConfig,
        provider: str,
        session_id: str,
        transcript_path: Path,
    ) -> TmuxSessionState:
        name = self._make_name(channel_key)
        return TmuxSessionState(
            session_name=name,
            session_dir=str(self._sessions_dir / name),
            session_id=session_id,
            mode=runtime.mode,
            cwd=str(runtime.cwd),
            mcp_config=runtime.mcp_config or "",
            chat_id=channel_key[0],
            offset=0,
            runner_version=get_adapter(provider).runner_version_tag(),
            provider=provider,
            model=runtime.model,
            transcript_path=str(transcript_path) if provider == "codex" else None,
            base_mcp_config=runtime.mcp_config,
        )

    @staticmethod
    def _tmux_alive(session_name: str) -> bool:
        return _tmux_alive_fn(session_name)

    @staticmethod
    def _file_size(path: Path) -> int:
        return _file_size_fn(path)

    def _save_state(self) -> None:
        """Persist sessions via the thread-safe StateStore.

        Wave 3 B3: the store guards `tmp → os.replace` with a
        `threading.Lock`, so concurrent tail-loop saves no longer race.
        """
        self._state_store.save(self._sessions)

    async def _locate_transcript_after_send(
        self,
        channel_key: ChannelKey,
        state: TmuxSessionState,
        adapter: ProviderAdapter,
    ) -> None:
        """Locate TUI transcript after sending the first message.

        Uses the start snapshot to find NEW transcripts since session start.
        Both Codex and Nessy generate their own session IDs and write
        transcripts only after processing the first user prompt.
        """
        existing, since_wall = self._tui_start_snapshots.get(channel_key, (set(), time.time()))
        try:
            info = await adapter.locate_tui_transcript(  # type: ignore[attr-defined]
                cwd=state.cwd,
                existing=existing,
                since_wall_time=since_wall,
                timeout_sec=30.0,
            )
        except Exception:
            raise RuntimeError(
                f"{engine_display_name(state.provider)} TUI transcript discovery failed"
            ) from None
        state.session_id = info.session_id
        state.transcript_path = str(info.transcript_path)
        self._tui_start_snapshots.pop(channel_key, None)
        self._save_state()

    def _transcript_path_for_state(self, state: TmuxSessionState) -> Path | None:
        if state.provider == "codex":
            if not state.transcript_path:
                return None
            return Path(state.transcript_path)
        if state.provider == "nessy":
            if not state.transcript_path:
                return None
            return Path(state.transcript_path)
        if not state.session_id:
            return None
        return transcript_path(state.cwd, state.session_id)

    @staticmethod
    def _find_codex_transcript(session_id: str, cwd: str) -> Path | None:
        return CODEX_ADAPTER.find_tui_transcript(cwd=cwd, session_id=session_id)

    # Public alias for callers outside the class (shutdown handlers, etc.).
    # External code should not poke at `_save_state` directly.
    def persist_state(self) -> None:
        """Persist tmux session state. Safe to call from outside the class."""
        self._save_state()

    async def _tail_until_done(
        self,
        output_path: Path,
        state: TmuxSessionState,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
        cancel_event: asyncio.Event,
        *,
        idle_exit_sec: float | None = None,
    ) -> tuple[str, str | None]:
        """Thin wrapper around TailRunner (W2.3 extraction).

        Owns the two pieces of context that a TailRunner cannot compute on
        its own: the channel_key for the given state (for rotation WARN
        context), and the existence deadline — shared with the spawn-readiness
        budget via ``_spawn_deadlines`` (Decision 7).

        A popped deadline in the past is ignored — ``/new`` (clear_context) and
        reply-to-resume (switch_session) call ``_spawn_tmux`` too, but the next
        user message can arrive minutes later, leaving the shared clock stale
        before the tail starts.
        """
        start_time = time.monotonic()
        channel_key = self._find_channel_key(state)
        existence_deadline: float = start_time + _SPAWN_READINESS_BUDGET_SEC
        if channel_key is not None:
            popped = self._spawn_deadlines.pop(channel_key, None)
            if popped is not None and popped > start_time:
                existence_deadline = popped

        runner = TailRunner(
            channel_key=channel_key,
            state=state,
            output_path=output_path,
            on_event=self._wrap_on_event_api_error_retry(channel_key, state, on_event),
            cancel_event=cancel_event,
            save_state=self._save_state,
            tmux_alive=self._tmux_alive,
            existence_deadline=existence_deadline,
            idle_exit_sec=idle_exit_sec,
        )
        return await runner.run()

    def _wrap_on_event_api_error_retry(
        self,
        channel_key: ChannelKey | None,
        state: TmuxSessionState,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> Callable[[StreamEvent], Awaitable[None]]:
        """Intercept `api_error` StreamEvents on every tail path.

        Applied inside `_tail_until_done`, so both `send_stream` turns and
        recovery tails get the same behavior. Consumers downstream never see
        the `api_error` event type — it is translated here into either a
        retry nudge + status line, or the raw error text (pre-feature
        rendering). Any other event counts as forward progress and resets the
        channel's retry budget.
        """

        async def wrapped(event: StreamEvent) -> None:
            if event.type == "api_error":
                await self._handle_api_error_event(channel_key, state, event, on_event)
                return
            if channel_key is not None:
                self._api_error_retry.note_progress(channel_key)
            ret = on_event(event)
            if asyncio.iscoroutine(ret):
                await ret

        return wrapped

    async def _handle_api_error_event(
        self,
        channel_key: ChannelKey | None,
        state: TmuxSessionState,
        event: StreamEvent,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> None:
        """React to an `isApiErrorMessage` transcript entry.

        Claude Code does not retry 400-class API errors itself — it prints
        the error and idles at the prompt. For known-transient errors (see
        `api_error_retry.RETRYABLE_API_ERROR_PATTERNS`) the session heals on
        any follow-up message, so we nudge the pane with the configured
        retry prompt, up to the consecutive-retry budget. Everything else —
        non-retryable errors, exhausted budget, or a failed nudge delivery —
        falls through to the pre-feature behavior of surfacing the raw error
        text in chat.
        """

        async def dispatch(forwarded: StreamEvent) -> None:
            ret = on_event(forwarded)
            if asyncio.iscoroutine(ret):
                await ret

        attempt: int | None = None
        if (
            channel_key is not None
            and state.provider == "claude"
            and is_retryable_api_error(event.content)
        ):
            attempt = self._api_error_retry.register_error(channel_key)

        if attempt is not None and channel_key is not None:
            limit = self._api_error_retry.limit
            logger.warning(
                "TUI_IO: api_error autoretry session=%s attempt=%d/%d error=%r",
                state.session_name,
                attempt,
                limit,
                event.content[:200],
            )
            await dispatch(
                StreamEvent("status", t("ui.api_error_retry", attempt=attempt, limit=limit))
            )
            # Let the TUI finish rendering the error and settle back at the
            # input prompt before pasting the nudge; back off exponentially
            # across attempts (2s → 4s → … → 20s cap) so upstream incidents
            # that need tens of seconds to clear don't burn the whole budget.
            await asyncio.sleep(_api_error_nudge_delay_sec(attempt))
            async with self._get_channel_lock(channel_key):
                delivered = await self._safe_send_and_enter(
                    channel_key, state, self._api_error_retry_prompt
                )
            if delivered:
                return
            # The nudge did not land (a modal alert was already posted by the
            # send helper) — fall through so the user still sees what failed.

        await dispatch(StreamEvent("text", event.content))

    def _find_channel_key(self, state: TmuxSessionState) -> ChannelKey | None:
        """Reverse-lookup channel_key for a given state (cheap, dict is small)."""
        for key, candidate in self._sessions.items():
            if candidate is state:
                return key
        return None
