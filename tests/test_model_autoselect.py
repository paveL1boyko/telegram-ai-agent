"""Tests for per-topic model autoselection."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.model_autoselect import (
    AUTO_MODEL,
    ModelAutoselect,
    canonical_model,
    is_auto_model,
)
from telegram_bot.core.services.providers import _model_argv

POOL = ("claude-opus-5[1m]", "claude-opus-4-7[1m]", "claude-fable-5[1m]")


def _select(tmp_path: Path, pool: tuple[str, ...] = POOL, **kwargs: object) -> ModelAutoselect:
    """Autoselect with a pre-filled pool and no real command behind it."""
    selector = ModelAutoselect(
        command="/bin/true",
        ledger_path=tmp_path / "model_assign.json",
        **kwargs,  # type: ignore[arg-type]
    )
    selector._pool = pool
    selector._pool_at = float("inf")  # never stale, so no refresh is scheduled
    return selector


def _stub_script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "healthy.sh"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_canonical_model_collapses_spellings_of_one_quota() -> None:
    assert canonical_model("claude-opus-5") == "claude-opus-5"
    assert canonical_model("anthropic/claude-opus-5[1m]") == "claude-opus-5"
    assert canonical_model("openrouter/anthropic/claude-opus-5") == "claude-opus-5"
    assert canonical_model("claude-fable-5[1m]") != canonical_model("claude-opus-5[1m]")


def test_is_auto_model_accepts_only_the_sentinel() -> None:
    assert is_auto_model("auto")
    assert is_auto_model("  AUTO  ")
    assert not is_auto_model("claude-opus-5")
    assert not is_auto_model(None)
    assert not is_auto_model("autopilot")


def test_resolve_passes_through_everything_but_the_sentinel(tmp_path: Path) -> None:
    selector = _select(tmp_path)

    assert selector.resolve(1, "claude-fable-5[1m]") == "claude-fable-5[1m]"
    assert selector.resolve(1, None) is None


def test_resolve_fails_open_without_a_pool_or_command(tmp_path: Path) -> None:
    empty_pool = _select(tmp_path, pool=())
    assert empty_pool.resolve(1, AUTO_MODEL) is None

    disabled = ModelAutoselect(command="  ", ledger_path=tmp_path / "ledger.json")
    assert not disabled.enabled
    assert disabled.resolve(1, AUTO_MODEL) is None


def test_topics_spread_across_distinct_models_then_wrap(tmp_path: Path) -> None:
    selector = _select(tmp_path)

    first = selector.resolve(101, AUTO_MODEL)
    second = selector.resolve(102, AUTO_MODEL)
    third = selector.resolve(103, AUTO_MODEL)

    assert {first, second, third} == set(POOL)
    assert first == POOL[0]  # pool order is the tiebreak, so the best goes first

    # a fourth topic has no free model left and lands on the least loaded one
    assert selector.resolve(104, AUTO_MODEL) == POOL[0]


def test_assignment_is_sticky_while_the_model_stays_healthy(tmp_path: Path) -> None:
    selector = _select(tmp_path)

    chosen = selector.resolve(101, AUTO_MODEL)
    selector.resolve(102, AUTO_MODEL)

    # resolve() runs on every message: the answer must not drift
    assert selector.resolve(101, AUTO_MODEL) == chosen
    assert selector.resolve(101, AUTO_MODEL) == chosen


def test_topic_is_repicked_when_its_model_leaves_the_pool(tmp_path: Path) -> None:
    selector = _select(tmp_path)
    chosen = selector.resolve(101, AUTO_MODEL)
    assert chosen == POOL[0]

    selector._pool = POOL[1:]
    repicked = selector.resolve(101, AUTO_MODEL)

    assert repicked != chosen
    assert repicked in POOL[1:]


def test_ledger_survives_a_restart(tmp_path: Path) -> None:
    ledger = tmp_path / "model_assign.json"
    selector = _select(tmp_path)
    chosen = selector.resolve(101, AUTO_MODEL)

    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert saved["topics"]["101"]["model"] == chosen

    restarted = _select(tmp_path)
    assert restarted.resolve(101, AUTO_MODEL) == chosen
    # and the restored assignment still counts as load for other topics
    assert restarted.resolve(102, AUTO_MODEL) != chosen


def test_unreadable_ledger_starts_empty_instead_of_raising(tmp_path: Path) -> None:
    ledger = tmp_path / "model_assign.json"
    ledger.write_text("{not json", encoding="utf-8")

    selector = _select(tmp_path)

    assert selector.assignments() == {}
    assert selector.resolve(101, AUTO_MODEL) == POOL[0]


def test_release_frees_the_assignment(tmp_path: Path) -> None:
    selector = _select(tmp_path)
    selector.resolve(101, AUTO_MODEL)

    selector.release(101)

    assert selector.assignments() == {}


def test_parse_validates_and_deduplicates_command_output() -> None:
    parsed = ModelAutoselect._parse(
        "\n".join(
            [
                "claude-opus-5[1m]",
                "  anthropic/claude-opus-5[1m]  ",  # same quota, dropped
                "--dangerously-skip-permissions",  # argv-looking, rejected
                "",
                "auto",  # the sentinel can never be a pool entry
                "claude-fable-5",
            ]
        )
    )

    assert parsed == ("claude-opus-5[1m]", "claude-fable-5")


async def test_refresh_reads_the_command_output(tmp_path: Path) -> None:
    command = _stub_script(tmp_path, "echo claude-opus-5[1m]; echo claude-fable-5[1m]")
    selector = ModelAutoselect(command=command, ledger_path=tmp_path / "ledger.json")

    pool = await selector.refresh()

    assert pool == ("claude-opus-5[1m]", "claude-fable-5[1m]")
    assert selector.resolve(101, AUTO_MODEL) == "claude-opus-5[1m]"


async def test_failing_command_keeps_the_previous_pool(tmp_path: Path) -> None:
    command = _stub_script(tmp_path, "echo boom >&2; exit 3")
    selector = ModelAutoselect(command=command, ledger_path=tmp_path / "ledger.json")
    selector._pool = POOL
    selector._pool_at = 1.0

    assert await selector.refresh() == POOL
    assert selector.resolve(101, AUTO_MODEL) in POOL


async def test_missing_command_disables_assignment_without_raising(tmp_path: Path) -> None:
    selector = ModelAutoselect(
        command=str(tmp_path / "does-not-exist"),
        ledger_path=tmp_path / "ledger.json",
    )

    assert await selector.refresh() == ()
    assert selector.resolve(101, AUTO_MODEL) is None


async def test_slow_command_times_out_and_leaves_the_pool_empty(tmp_path: Path) -> None:
    command = _stub_script(tmp_path, "sleep 5")
    selector = ModelAutoselect(
        command=command,
        ledger_path=tmp_path / "ledger.json",
        timeout_sec=0.2,
    )

    assert await selector.refresh() == ()
    assert selector.resolve(101, AUTO_MODEL) is None


def test_model_argv_never_passes_the_sentinel_to_a_cli() -> None:
    assert _model_argv("claude-fable-5[1m]") == ["--model", "claude-fable-5[1m]"]
    assert _model_argv(None) == []
    assert _model_argv(AUTO_MODEL) == []
    assert _model_argv(" Auto ") == []


def test_session_manager_resolves_auto_through_the_selector(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    selector = _select(tmp_path)
    manager = SessionManager(settings, model_autoselect=selector)

    assert manager.model_autoselect_enabled
    assert manager.resolve_model(101, AUTO_MODEL) == POOL[0]
    assert manager.resolve_model(101, "claude-fable-5") == "claude-fable-5"


def test_session_manager_without_autoselect_drops_the_sentinel() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    manager = SessionManager(settings)

    assert not manager.model_autoselect_enabled
    assert manager.resolve_model(101, AUTO_MODEL) is None
    assert manager.resolve_model(101, "claude-fable-5") == "claude-fable-5"


def test_ledger_is_written_atomically(tmp_path: Path) -> None:
    ledger = tmp_path / "nested" / "model_assign.json"
    selector = ModelAutoselect(command="/bin/true", ledger_path=ledger)
    selector._pool = POOL
    selector._pool_at = float("inf")

    selector.resolve(101, AUTO_MODEL)

    assert ledger.exists()
    assert not any(p.suffix == ".tmp" for p in ledger.parent.iterdir())
    assert os.access(ledger, os.R_OK)
