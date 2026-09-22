"""Per-topic model autoselection driven by an external health command.

The bot never probes a gateway itself. `MODEL_AUTOSELECT_CMD` names a command
that prints the model ids the gateway currently serves — one per line, best
first. Everything provider-specific (endpoints, auth, which HTTP error means
"dead route" rather than "throttled") stays in that command, which keeps this
module generic and lets tests hand it a stub script.

A topic opts in by pinning the literal model `auto`, offered as the first row of
the `/model` picker. Resolution deliberately:

* is synchronous — it runs inside `_apply_topic_config` on every session lookup,
  so it may not await anything; the pool is refreshed by a background task;
* fails open — an empty pool (command missing, slow, or broken) resolves to
  `None`, which emits no `--model` flag at all and leaves the engine on its own
  default. A model picker is not worth blocking a topic from starting;
* is sticky per topic while the chosen model stays healthy — `resolve()` is
  called for every message, and re-picking mid-conversation would make a resumed
  CLI session come back on a different model.

Load is spread across *canonical* models: `claude-opus-5` and
`anthropic/claude-opus-5` are two spellings of one upstream quota, so counting
them separately would hand two topics the same bucket while believing they were
balanced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from telegram_bot.core.services.topic_config import normalize_model_override

logger = logging.getLogger(__name__)

# Sentinel a topic pins to join the rotation. It is a valid model-override
# string syntactically, so it survives topic_config validation and round-trips
# through topic_config.json like any other value — but it must never reach a CLI
# as `--model auto`, which is why the argv builders drop it defensively.
AUTO_MODEL = "auto"

_DEFAULT_REFRESH_SEC = 600.0
_DEFAULT_TIMEOUT_SEC = 30.0
_DEFAULT_ASSIGN_TTL_SEC = 7 * 86400.0


def is_auto_model(model: str | None) -> bool:
    """True when `model` is the autoselect sentinel rather than a real id."""
    return model is not None and model.strip().lower() == AUTO_MODEL


def canonical_model(model: str) -> str:
    """Identity used for load spreading: no context marker, no routing prefix.

    `anthropic/claude-opus-5[1m]`, `claude-opus-5` and
    `openrouter/anthropic/claude-opus-5` all collapse to `claude-opus-5`. That
    is conservative on purpose: when two routes might share one upstream quota,
    treating them as one bucket loses a little spread, while treating them as
    two would silently double-book the same quota.
    """
    base = model.split("[", 1)[0].strip().rstrip("/")
    return base.rsplit("/", 1)[-1].lower()


@dataclass(frozen=True)
class Assignment:
    """Which model a topic is currently parked on."""

    model: str
    canonical: str
    assigned_at: float


class ModelAutoselect:
    """Assigns healthy models to topics, spreading them across distinct quotas."""

    def __init__(
        self,
        *,
        command: str,
        ledger_path: Path | str,
        refresh_sec: float = _DEFAULT_REFRESH_SEC,
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
        assign_ttl_sec: float = _DEFAULT_ASSIGN_TTL_SEC,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._command = command.strip()
        self._ledger_path = Path(ledger_path)
        self._refresh_sec = refresh_sec
        self._timeout_sec = timeout_sec
        self._assign_ttl_sec = assign_ttl_sec
        self._now = now
        self._pool: tuple[str, ...] = ()
        self._pool_at: float = 0.0
        self._refreshing = False
        self._refresh_task: asyncio.Task[None] | None = None
        self._assignments: dict[int, Assignment] = {}
        self._load()

    # ------------------------------------------------------------------ state --

    @property
    def enabled(self) -> bool:
        return bool(self._command)

    @property
    def pool(self) -> tuple[str, ...]:
        """Healthy models as last reported, best first."""
        return self._pool

    def assignments(self) -> dict[int, Assignment]:
        """Copy of the topic → model ledger, for diagnostics."""
        return dict(self._assignments)

    def _load(self) -> None:
        try:
            raw = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning(
                "model_autoselect: unreadable ledger %s, starting empty",
                self._ledger_path,
                exc_info=True,
            )
            return
        topics = raw.get("topics") if isinstance(raw, dict) else None
        if not isinstance(topics, dict):
            return
        for key, value in topics.items():
            try:
                thread_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            model = normalize_model_override(value.get("model"))
            if model is None or is_auto_model(model):
                continue
            try:
                assigned_at = float(value.get("assigned_at", 0.0))
            except (TypeError, ValueError):
                assigned_at = 0.0
            self._assignments[thread_id] = Assignment(
                model=model,
                canonical=canonical_model(model),
                assigned_at=assigned_at,
            )

    def _save(self) -> None:
        cutoff = self._now() - self._assign_ttl_sec
        live = {
            str(thread_id): {
                "model": a.model,
                "canonical": a.canonical,
                "assigned_at": a.assigned_at,
            }
            for thread_id, a in self._assignments.items()
            if a.assigned_at >= cutoff
        }
        payload = {"updated_at": self._now(), "topics": live}
        tmp = self._ledger_path.with_suffix(self._ledger_path.suffix + ".tmp")
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._ledger_path)
        except OSError:
            logger.warning(
                "model_autoselect: cannot write ledger %s", self._ledger_path, exc_info=True
            )
            tmp.unlink(missing_ok=True)

    # ----------------------------------------------------------------- refresh --

    async def prime(self) -> None:
        """Fill the pool before the first message needs it."""
        if self.enabled:
            await self.refresh()

    async def refresh(self) -> tuple[str, ...]:
        """Run the health command and replace the pool with what it reports."""
        if not self.enabled:
            return ()
        argv = shlex.split(self._command)
        if not argv:
            return ()
        self._refreshing = True
        try:
            pool = await self._run(argv)
        finally:
            self._refreshing = False
        # A failed refresh keeps the previous pool: a transient probe failure
        # should not strip every topic of its model.
        if pool:
            self._pool = pool
            self._pool_at = self._now()
        return self._pool

    async def _run(self, argv: list[str]) -> tuple[str, ...]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.warning("model_autoselect: cannot run %r", self._command, exc_info=True)
            return ()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self._timeout_sec)
        except TimeoutError:
            logger.warning(
                "model_autoselect: %r timed out after %ss", self._command, self._timeout_sec
            )
            await self._terminate(proc)
            return ()
        if proc.returncode != 0:
            logger.warning(
                "model_autoselect: %r exited %s: %s",
                self._command,
                proc.returncode,
                stderr.decode("utf-8", "replace").strip()[:200],
            )
            return ()
        return self._parse(stdout.decode("utf-8", "replace"))

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        """Best-effort cleanup of a timed-out child.

        The signal can be refused (a sandboxed runtime raises PermissionError)
        or the child may have exited in the meantime (ProcessLookupError).
        Neither turns a timeout into a crash: the worst case is an abandoned
        child, while an exception here would take down the refresh task.
        """
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 1.0)

    @staticmethod
    def _parse(stdout: str) -> tuple[str, ...]:
        """Validated model ids from the command's stdout, order preserved.

        Every line goes through the same normalizer as a user-pinned override,
        so a misbehaving script cannot inject argv-looking values.
        """
        models: list[str] = []
        seen: set[str] = set()
        for line in stdout.splitlines():
            model = normalize_model_override(line)
            if model is None or is_auto_model(model):
                continue
            canonical = canonical_model(model)
            if canonical in seen:
                continue
            seen.add(canonical)
            models.append(model)
        return tuple(models)

    def _maybe_schedule_refresh(self) -> None:
        """Kick off a refresh when the pool is stale, without blocking."""
        if self._refreshing:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        if self._pool and self._now() - self._pool_at < self._refresh_sec:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # no loop (tests, sync context): the next call inside the bot's loop
            # will schedule it
            return
        self._refresh_task = loop.create_task(self._background_refresh())

    async def _background_refresh(self) -> None:
        try:
            await self.refresh()
        except Exception:
            logger.debug("model_autoselect: background refresh failed", exc_info=True)

    # ----------------------------------------------------------------- resolve --

    def resolve(self, thread_id: int | None, model: str | None) -> str | None:
        """Turn the `auto` sentinel into a concrete model id.

        Any other value — including `None` — is returned untouched, so call
        sites can route every model through here unconditionally.
        """
        if not is_auto_model(model):
            return model
        if not self.enabled or thread_id is None:
            return None
        self._maybe_schedule_refresh()
        pool = self._pool
        if not pool:
            return None
        current = self._assignments.get(thread_id)
        if current is not None and current.model in pool:
            return current.model
        chosen = self._least_loaded(thread_id, pool)
        self._assignments[thread_id] = Assignment(
            model=chosen,
            canonical=canonical_model(chosen),
            assigned_at=self._now(),
        )
        self._save()
        logger.info(
            "model_autoselect: thread_id=%s -> %s (pool=%s)",
            thread_id,
            chosen,
            ", ".join(pool),
        )
        return chosen

    def _least_loaded(self, thread_id: int, pool: tuple[str, ...]) -> str:
        """Pool entry backing the fewest other topics; pool order breaks ties.

        Strict round-robin would run out once there are more topics than
        healthy models; least-loaded keeps spreading and degrades to two topics
        per model instead of refusing to assign.
        """
        load: dict[str, int] = {}
        for other_id, assignment in self._assignments.items():
            if other_id == thread_id:
                continue
            load[assignment.canonical] = load.get(assignment.canonical, 0) + 1
        ranked = sorted(
            enumerate(pool),
            key=lambda item: (load.get(canonical_model(item[1]), 0), item[0]),
        )
        return ranked[0][1]

    def release(self, thread_id: int) -> None:
        """Drop a topic's assignment so its next start re-picks from the pool."""
        if self._assignments.pop(thread_id, None) is not None:
            self._save()
