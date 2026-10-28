"""Auto-approval of Claude Code tool-permission dialogs.

Covers the two layers of the feature:

1. `tui/permission_prompt.parse_permission_prompt` — structural detection
   of the "Do you want to proceed?" dialog and tool-name extraction from
   the managed-policy line / classic wording / menu option 2 / tool-call
   header fallback, including scrollback-ghost rejection (a resolved
   dialog persists above the redrawn input box and must not parse);
2. the TmuxManager watchdog hook — Enter on the verified dialog for
   operator-allowlisted tools only, with re-capture guards on both sides
   of the keypress, bounded retries when an Enter is swallowed mid-render,
   and fall-through to the manual idle alert on failure;
3. the same approval inside the user-send pipeline
   (`_send_with_modal_guard`) — the watchdog skips a channel while its
   send lock is held, so a message typed while the dialog is up used to
   turn into a manual "modal before send" alert (2026-10-28).
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

# A resolved dialog stays visible in scrollback while CC redraws its real
# UI below it — input-box sandwich + status line. Captured from a live
# session (2026-10-27): the watchdog parsed this ghost as a live dialog
# and sent Enter into the user's input box on every tick.
GHOST_ABOVE_INPUT_BOX = (
    WEBSEARCH_DIALOG
    + "\n"
    + "─" * 60
    + "\n❯ \n"
    + "─" * 60
    + "\n"
    + "  proj  main | model | $1.00 session | ctx:~10k/200k\n"
    + "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
)

# Same ghost, but with user text typed into the input box (`❯ 2` — seen
# live when a bogus ghost-alert's button typed "2" into the box).
GHOST_WITH_TYPED_INPUT = (
    WEBSEARCH_DIALOG + "\n" + "─" * 60 + "\n❯ 2\n" + "─" * 60 + "\n  proj  main\n"
)

# Ghost above the busy line (CC already working after the dialog resolved).
GHOST_ABOVE_BUSY_LINE = WEBSEARCH_DIALOG + "\n⏺ Working on it…\n  esc to interrupt\n"


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


def test_parse_rejects_scrollback_ghost_above_input_box() -> None:
    """A resolved dialog persists in scrollback; with CC's input box
    rendered below it, it must NOT parse — acting on the ghost sends Enter
    into the user's input box on every watchdog tick (incident 2026-10-27)."""
    assert parse_permission_prompt(GHOST_ABOVE_INPUT_BOX) is None


def test_parse_rejects_ghost_with_typed_text_in_input() -> None:
    assert parse_permission_prompt(GHOST_WITH_TYPED_INPUT) is None


def test_parse_rejects_ghost_above_busy_line() -> None:
    assert parse_permission_prompt(GHOST_ABOVE_BUSY_LINE) is None


def test_parse_live_dialog_below_ghost_uses_own_rule_line() -> None:
    """Ghost above, live dialog at the bottom: the bottom-most question
    wins, and the tool comes from the LIVE dialog's own rule line — the
    old first-match-in-tail logic would have returned the ghost's tool."""
    pane = WEBFETCH_ALLOW_DIALOG + "\n⏺ Done thinking\n" + WEBSEARCH_DIALOG
    assert parse_permission_prompt(pane) == "websearch"


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
    """Dialog still on screen after every Enter attempt (keypresses lost /
    new dialog for the same tool each time) — bounded retries exhaust and
    the user must get the manual alert."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, WEBSEARCH_DIALOG])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == ["cc-1-2"] * 3
    assert alerts == [WEBSEARCH_DIALOG]


def test_watchdog_retries_enter_lost_mid_render(tmp_path: Path, monkeypatch) -> None:
    """First Enter swallowed (dialog caught mid-render), the retry resolves:
    two Enters, one approval, no alert."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(
        monkeypatch,
        [WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, IDLE_PANE],
    )
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == ["cc-1-2"] * 2
    assert alerts == []
    assert manager._last_modal_pane[(1, 2)] == WEBSEARCH_DIALOG


def test_watchdog_ignores_scrollback_ghost(tmp_path: Path, monkeypatch) -> None:
    """Ghost above the input box: no Enter, no alert — before the fix the
    watchdog pressed Enter into the user's input box every tick (2026-10-27)."""
    manager, _state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_io(monkeypatch, [GHOST_ABOVE_INPUT_BOX])
    alerts = _patch_alert(monkeypatch, manager)

    asyncio.run(manager._check_channel_modal((1, 2)))

    assert calls["enter"] == []
    assert alerts == []


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


# --- send pipeline: approval instead of "modal before send" ------------------


def _patch_send_pipeline(monkeypatch, manager: TmuxManager, panes: list[str]) -> dict[str, list]:
    """Fake every tmux I/O of `_send_with_modal_guard` + record alerts."""
    calls = _patch_io(monkeypatch, panes)
    calls["paste"] = []
    calls["ctrl_u"] = []
    calls["alerts"] = []

    async def fake_paste(session_name: str, text: str, *, submit_enter: bool = True) -> None:
        calls["paste"].append(text)

    async def fake_ctrl_u(session_name: str) -> None:
        calls["ctrl_u"].append(session_name)

    async def fake_alert(channel_key, state, prompt, pane, *, reason):  # type: ignore[no-untyped-def]
        calls["alerts"].append(reason)

    monkeypatch.setattr(tmux_manager_module, "send_text_to_tmux", fake_paste)
    monkeypatch.setattr(tmux_manager_module, "send_ctrl_u", fake_ctrl_u)
    monkeypatch.setattr(tmux_manager_module, "_GUARD_SETTLE_SHORT_SEC", 0)
    monkeypatch.setattr(tmux_manager_module, "_GUARD_SETTLE_LONG_SEC", 0)
    monkeypatch.setattr(manager, "_send_modal_alert", fake_alert)
    return calls


def test_send_approves_allowlisted_dialog_before_paste(tmp_path: Path, monkeypatch) -> None:
    """The 2026-10-28 incident: user message arrives while CC shows the
    WebSearch dialog. Expected: Enter on the dialog, then the normal
    paste + Enter of the message — no alert."""
    manager, state = _make_manager(tmp_path, tools=["WebSearch"])
    # pre-send capture → verify capture → post-Enter capture (dialog gone)
    # → re-capture pre-send → post-paste capture.
    calls = _patch_send_pipeline(
        monkeypatch,
        manager,
        [WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, IDLE_PANE, IDLE_PANE, IDLE_PANE],
    )

    ok = asyncio.run(manager._send_with_modal_guard((1, 2), state, "hello"))

    assert ok is True
    assert calls["paste"] == ["hello"]
    # One Enter for the dialog, one for the message.
    assert calls["enter"] == ["cc-1-2", "cc-1-2"]
    assert calls["alerts"] == []


def test_send_still_alerts_on_non_allowlisted_dialog(tmp_path: Path, monkeypatch) -> None:
    manager, state = _make_manager(tmp_path, tools=["WebSearch"])
    calls = _patch_send_pipeline(monkeypatch, manager, [BASH_DIALOG])

    ok = asyncio.run(manager._send_with_modal_guard((1, 2), state, "hello"))

    assert ok is False
    assert calls["paste"] == []
    assert calls["enter"] == []
    assert calls["alerts"] == ["claude_modal_before_send"]


def test_send_repastes_when_dialog_pops_after_paste(tmp_path: Path, monkeypatch) -> None:
    """Dialog appears between the pre-check and the post-paste check: the
    paste is cleared (C-u), the dialog approved, and the message pasted
    again before Enter."""
    manager, state = _make_manager(tmp_path, tools=["WebSearch"])
    # pre-send (idle) → post-paste (dialog) → verify (dialog) → post-Enter
    # (idle) → post-second-paste (idle).
    calls = _patch_send_pipeline(
        monkeypatch,
        manager,
        [IDLE_PANE, WEBSEARCH_DIALOG, WEBSEARCH_DIALOG, IDLE_PANE, IDLE_PANE],
    )

    ok = asyncio.run(manager._send_with_modal_guard((1, 2), state, "hello"))

    assert ok is True
    assert calls["paste"] == ["hello", "hello"]
    assert calls["ctrl_u"] == ["cc-1-2"]
    assert calls["enter"] == ["cc-1-2", "cc-1-2"]
    assert calls["alerts"] == []
