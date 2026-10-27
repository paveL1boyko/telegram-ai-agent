"""Detection of Claude Code tool-permission dialogs for bot-side auto-approval.

WHY this exists
---------------
Some managed Claude Code builds force an interactive "Do you want to
proceed?" dialog on every call of certain read-only tools (WebSearch,
WebFetch): the signed policy's ask-list cannot be lifted by user
``permissions.allow`` settings, so every search stalls the session until
someone presses "1. Yes" — painful when the session is driven remotely
through this Telegram bot.

The bot cannot change the CLI's policy, but the operator CAN pre-authorize
a narrow allowlist of tools (``Settings.auto_approve_permission_tools``):
when the modal watchdog sees a *verified* permission dialog for an
allowlisted tool, it answers "1. Yes" itself and posts a notice to chat.
Everything not on the list keeps the manual alert flow.

Detection is deliberately structural and strict — ALL of these must hold
in the pane tail:

1. the question line (``Do you want to proceed?``, or the per-tool
   variant ``Do you want to allow Claude to …?`` some builds use);
2. the selection cursor on the first option (``❯ 1. Yes``);
3. the full menu (a ``3. No`` line);
4. an extractable tool name.

Enter is only ever sent after a fresh re-capture re-confirms the exact
same dialog, so a stale watchdog snapshot can never bleed an Enter into an
unrelated modal (see modal_detect.py for why blind Enters are dangerous).
"""

from __future__ import annotations

import re

# How many trailing pane lines we scan for the dialog. The permission
# dialog renders as a ~10-line block (tool-call echo, question, menu,
# footer); a wider window tolerates the tool-call echo above it without
# reaching deep into scrollback.
_DIALOG_SCAN_LINES = 40

# The question line. Most builds ask "Do you want to proceed?"; some
# phrase it per-tool, e.g. "Do you want to allow Claude to fetch this
# content?" (seen on a managed build, 2026-10-26). Both name the same
# modal — accept either so a CLI build update can't silently disable
# auto-approval.
_PROCEED_RE = re.compile(r"Do you want to (?:proceed|allow Claude to [^\n?]{0,100})\?")

# Selection cursor on the first option. Enter confirms the cursor row, so
# this is the load-bearing check: without the cursor on "1. Yes" we must
# never press Enter.
_CURSOR_YES_RE = re.compile(r"^\s*❯\s*1\.\s*Yes\b", re.MULTILINE)

# The full three-option menu must be visible — a partial render (or a
# quoted fragment in scrollback) is not a dialog we touch.
_MENU_NO_RE = re.compile(r"^\s*3\.\s*No\b", re.MULTILINE)

# Tool-name sources, in priority order:
# 1. Managed-policy line: "Permission rule WebSearch requires confirmation
#    for this tool." — always present on the builds that force the dialog.
_PERMISSION_RULE_RE = re.compile(r"Permission rule (\S+) requires confirmation")
# 2. Classic wording: "Claude needs your permission to use WebSearch".
_NEEDS_PERMISSION_RE = re.compile(r"needs your permission to use (\w+)")
# 3. Menu option 2: "Yes, and don't ask again for Web Search commands in …".
_DONT_ASK_RE = re.compile(r"don't ask again for (.+?) commands? in ")
# 4. Last resort: the tool-call header directly above the question, e.g.
# ``Web Search("query")`` or ``⏺ WebFetch("https://…")``. Only matches
# before the question line, last match wins (see parse_permission_prompt).
_TOOL_HEADER_RE = re.compile(r"^\s*(?:⏺\s*)?([A-Z][A-Za-z0-9 ]{1,30}?)\(", re.MULTILINE)


def normalize_tool_name(name: str) -> str:
    """Canonical form for allowlist matching: ``Web Search`` / ``WebSearch``
    / ``web_search`` all become ``websearch``."""
    return re.sub(r"[\s_\-]+", "", name).lower()


def parse_permission_prompt(pane: str) -> str | None:
    """Return the normalized tool name when `pane` shows a Claude Code
    tool-permission dialog with the cursor on "1. Yes", else None.

    The returned name is lowercased with spaces/underscores stripped
    (``websearch``, ``webfetch``, ``bash``); allowlist filtering is the
    caller's job.
    """
    if not pane:
        return None
    tail = "\n".join(pane.splitlines()[-_DIALOG_SCAN_LINES:])
    question = _PROCEED_RE.search(tail)
    if question is None:
        return None
    if _CURSOR_YES_RE.search(tail) is None:
        return None
    if _MENU_NO_RE.search(tail) is None:
        return None
    for pattern in (_PERMISSION_RULE_RE, _NEEDS_PERMISSION_RE, _DONT_ASK_RE):
        m = pattern.search(tail)
        if m is not None:
            return normalize_tool_name(m.group(1))
    # Header fallback: restrict to the text BEFORE the question and take
    # the LAST match — the real dialog's tool-call header is the closest
    # one above the question; a stale "Web Search(...)" quoted higher in
    # scrollback must not mislabel a different tool's dialog.
    header = None
    for m in _TOOL_HEADER_RE.finditer(tail[: question.start()]):
        header = m
    if header is not None:
        return normalize_tool_name(header.group(1))
    return None
