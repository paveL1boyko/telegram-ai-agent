"""Auto-approval of Claude Code tool-permission dialogs.

Covers the two layers of the feature:

1. `tui/permission_prompt.parse_permission_prompt` — structural detection
   of the "Do you want to proceed?" dialog and tool-name extraction from
   the managed-policy line / classic wording / menu option 2 / tool-call
   header fallback;
2. the TmuxManager watchdog hook — Enter on the verified dialog for
   operator-allowlisted tools only, with re-capture guards on both sides
   of the keypress and fall-through to the manual idle alert on failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram_bot.core.config import Settings
from telegram_bot.core.services import tmux_manager as tmux_manager_module
from telegram_bot.core.services.tmux_manager import TmuxManager
from telegram_bot.core.services.tmux_state import TmuxSessionState
from telegram_bot.core.tui.permission_prompt import (
    normalize_tool_name,
    parse_permission_prompt,
)

# The managed-build dialog, captured verbatim from a tclaude session.
WEBSEARCH_DIALOG = """
 Tool use

   Web Search("site:graco.com FFLP514", only allowing domains: graco.com)
   │ Claude wants to search the web for: site:graco.com FFLP514

 Permission rule WebSearch requires confirmation for this tool.

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for Web Search commands in /Users/p.boyko/Work/ceneo
   3. No

 Esc to cancel
"""

BASH_DIALOG = """
⏺ Bash(rm -rf build/)
  ⎿  Running…

 Permission rule Bash requires confirmation for this tool.

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for Bash commands in /repo
   3. No

 Esc to cancel
"""

# Per-tool question wording, captured verbatim from a managed-build
# WebFetch dialog (2026-10-26): "Do you want to allow Claude to fetch
# this content?" instead of "Do you want to proceed?".
WEBFETCH_ALLOW_DIALOG = """
 Fetch

   url: https://www.ozon.ru/search/?text=Baoba+BZ-495
   │ prompt: Extract product titles, prices, and any specs
   Claude wants to fetch content from www.ozon.ru

 Permission rule WebFetch requires confirmation for this tool.

 Do you want to allow Claude to fetch this content?
 ❯ 1. Yes
   2. Yes, and don't ask again for www.ozon.ru
   3. No, and tell Claude what to do differently (esc)
"""

IDLE_PANE = """
⏺ Working on it…

────────────────────────────────────────
❯
────────────────────────────────────────
  esc to interrupt
"""


def test_parse_managed_build_websearch_dialog() -> None:
    assert parse_permission_prompt(WEBSEARCH_DIALOG) == "websearch"


def test_parse_managed_build_webfetch_dialog() -> None:
    pane = WEBSEARCH_DIALOG.replace("WebSearch", "WebFetch").replace("Web Search", "Web Fetch")
    assert parse_permission_prompt(pane) == "webfetch"


def test_parse_allow_claude_question_wording() -> None:
    """Per-tool question variant ("Do you want to allow Claude to …?")
    must parse like the classic "Do you want to proceed?" dialog."""
    assert parse_permission_prompt(WEBFETCH_ALLOW_DIALOG) == "webfetch"


def test_parse_classic_needs_permission_wording() -> None:
    pane = (
        " Claude needs your permission to use WebFetch\n"
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again for WebFetch commands in /repo\n"
        "   3. No\n"
    )
    assert parse_permission_prompt(pane) == "webfetch"


def test_parse_dont_ask_menu_source() -> None:
    pane = (
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again for Web Search commands in /repo\n"
        "   3. No\n"
    )
    assert parse_permission_prompt(pane) == "websearch"


def test_parse_header_fallback_prefers_closest_header() -> None:
    """With no permission-rule/don't-ask wording, the tool comes from the
    tool-call header — the LAST one before the question, so stale
    scrollback higher up cannot mislabel the dialog."""
    pane = (
        '⏺ Web Search("stale scrollback query")\n'
        "  ⎿  done\n" + "filler line\n" * 10 + '⏺ WebFetch("https://example.com")\n'
        "\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again this session\n"
        "   3. No\n"
    )
    assert parse_permission_prompt(pane) == "webfetch"


def test_parse_bash_dialog_detected_for_caller_to_filter() -> None:
    """The detector is tool-agnostic; allowlist filtering is the manager's
    job. A Bash dialog must still *parse* so the fall-through alert path
    treats it as a modal even without footer tokens."""
    assert parse_permission_prompt(BASH_DIALOG) == "bash"


def test_parse_rejects_cursor_not_on_yes() -> None:
    pane = WEBSEARCH_DIALOG.replace("❯ 1. Yes", "  1. Yes\n ❯ 2. Yes, and don't ask again")
    assert parse_permission_prompt(pane) is None


def test_parse_rejects_incomplete_menu() -> None:
    pane = WEBSEARCH_DIALOG.replace("   3. No", "")
    assert parse_permission_prompt(pane) is None


def test_parse_rejects_non_dialog_panes() -> None:
    assert parse_permission_prompt(IDLE_PANE) is None
    assert parse_permission_prompt("") is None
    # The question quoted in scrollback without a live menu is not a dialog.
    quoted = "Earlier CC asked: Do you want to proceed? and I said no"
    assert parse_permission_prompt(quoted) is None


def test_normalize_tool_name() -> None:
    assert normalize_tool_name("Web Search") == "websearch"
    assert normalize_tool_name("WebSearch") == "websearch"
    assert normalize_tool_name("web_search") == "websearch"
    assert normalize_tool_name("Web-Fetch ") == "webfetch"


def test_settings_default_auto_approve_disabled() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    assert settings.auto_approve_permission_tools == []


def _make_manager(
    tmp_path: Path,
    tools: list[str] | None = None,
    provider: str = "claude",
) -> tuple[TmuxManager, TmuxSessionState]:
    manager = TmuxManager(
        sessions_dir=tmp_path,
        auto_approve_permission_tools=tools or [],
    )
    state = TmuxSessionState(
        session_name="cc-1-2",
        session_dir=str(tmp_path / "cc-1-2"),
        session_id="session-abc",
        mode="free",
        cwd=".",
        mcp_config="",
        chat_id=1,
        provider=provider,
    )
    manager._sessions[(1, 2)] = state
    return manager, state


def _patch_io(monkeypatch, panes: list[str]) -> dict[str, list]:
    """Fake capture_pane (pops `panes`, then sticks on the last one) and
    send_enter (records calls)."""
    calls: dict[str, list] = {"enter": []}
    remaining = list(panes)

    async def fake_capture(session_name: str) -> str:
        if len(remaining) > 1:
            return remaining.pop(0)
        return remaining[0]

    async def fake_enter(session_name: str) -> None:
        calls["enter"].append(session_name)

    monkeypatch.setattr(tmux_manager_module, "capture_pane", fake_capture)
    monkeypatch.setattr(tmux_manager_module, "send_enter", fake_enter)
    monkeypatch.setattr(tmux_manager_module, "_PERMISSION_RESOLVE_WAIT_SEC", 0)
    return calls


def _patch_alert(monkeypatch, manager: TmuxManager) -> list:
    alerts: list = []

    async def fake_alert(channel_key, state, pane):
        alerts.append(pane)

    monkeypatch.setattr(manager, "_send_modal_idle_alert", fake_alert)
    return alerts


def test_watchdog_auto_approves_allowlisted_tool(tmp_path: Path, monkeypatch) -> None:
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, IDLE_PANE])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == ["cc-1-2"]
    assert alerts == []
    assert manager._last_modal_pane[(1, 2)] == WEBSEARCH_DIALOG


def test_watchdog_dedups_after_approval(tmp_path: Path, monkeypatch) -> None:
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    manager._last_modal_pane[(1, 2)] = WEBSEARCH_DIALOG  # approved on a prior tick
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == []


def test_watchdog_alerts_on_non_allowlisted_tool(tmp_path: Path, monkeypatch) -> None:
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [BASH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == [BASH_DIALOG]


def test_watchdog_disabled_by_default(tmp_path: Path, monkeypatch) -> None:
    manager, _state = _make_manager(tmp_path)  # empty allowlist
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == [WEBSEARCH_DIALOG]


def test_watchdog_skips_non_claude_provider(tmp_path: Path, monkeypatch) -> None:
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"], provider="codex")
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG])
    _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []


def test_enter_failure_falls_through_to_alert(tmp_path: Path, monkeypatch) -> None:
    """Dialog still on screen after Enter (keypress lost / new dialog for
    the same tool) — the user must get the manual alert."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, WEBSEARCH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == ["cc-1-2"]
    assert alerts == [WEBSEARCH_DIALOG]


def test_dialog_gone_between_ticks_needs_no_action(tmp_path: Path, monkeypatch) -> None:
    """The user pressed the button in the pane between the watchdog
    capture and our re-capture — no Enter, no alert."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG, IDLE_PANE])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == []
    assert (1, 2) not in manager._last_modal_pane


def test_dialog_switched_between_ticks_not_touched(tmp_path: Path, monkeypatch) -> None:
    """WebSearch dialog at capture time turned into a Bash dialog by
    re-capture — Enter must not fire; the alert path handles it."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG, BASH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == [WEBSEARCH_DIALOG]
