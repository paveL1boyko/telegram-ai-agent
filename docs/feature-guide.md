# Feature Guide: Where Things Live and How to Add One

Companion to `.claude/skills/project-knowledge/references/architecture.md`.
That file describes the runtime split; this one is a practical map for adding
a new command, picker, text, or MCP capability without rediscovering the
codebase each time.

## Code map

| Concern | Location |
| --- | --- |
| Entry point, DI wiring (`dp[...]`) | `src/telegram_bot/__main__.py` |
| Slash commands, inline pickers, callbacks | `src/telegram_bot/core/handlers/commands.py` |
| Free-text routing to the engine | `src/telegram_bot/core/handlers/text.py` |
| Media, voice, forwards, topic events | `src/telegram_bot/core/handlers/{photo,voice,forward,forum_topic}.py` |
| Inline keyboards | `src/telegram_bot/core/keyboards.py` |
| All user-facing strings (en + ru) | `src/telegram_bot/core/messages.py` |
| Settings and model presets | `src/telegram_bot/core/config.py` |
| Per-topic persisted config | `src/telegram_bot/core/services/topic_config.py` |
| Engine adapters (Claude/Codex specifics) | `src/telegram_bot/core/services/providers.py` |
| tmux session lifecycle | `src/telegram_bot/core/services/tmux_manager.py` |
| Agent → Telegram sends (MCP server) | `mcp-servers/bot/server.py` |
| Per-session MCP config generation | `src/telegram_bot/core/services/bot_mcp_runtime.py` |

## Adding a slash command with an inline picker

The established pattern (see `/model`, `/engine`, `/uimode` in
`core/handlers/commands.py`):

1. Keyboard builder in `core/keyboards.py` with a short callback prefix
   (`"model:"`, `"engine:"`). Callback data is limited to 64 bytes, so encode
   list choices as indexes, never full values, and re-validate the index
   against the live list in the handler — the keyboard may predate a config
   edit.
2. `_show_<x>_picker` helper posting the caption + keyboard; register it for
   both the `Command(...)` and the reply-keyboard button text
   (`F.text == t("ui.btn_<x>")`).
3. `@router.callback_query(F.data.startswith("<x>:"))` handler. Standard
   guards, in order: `callback.data`/`callback.message` present, not
   `InaccessibleMessage`, `message_thread_id` present (forum-only features),
   busy-check via `tmux_manager.is_processing(key)` /
   `message_queue.is_busy(key)` before mutating session state.
4. Persist through `topic_config.update_*`, then answer the callback and post
   a confirmation message.
5. Picker lifecycle: settings pickers (`/engine`, `/uimode`, stream/exec mode)
   re-render in place so the checkmark tracks the saved value; the model
   picker instead deletes itself after a successful choice
   (`_close_model_picker` — delete, fall back to stripping the keyboard when
   deletion fails, e.g. the message is older than 48h). Pick one behavior
   deliberately.

New handler dependencies (services) are injected by name: add the instance to
`dp["<name>"]` in `__main__.py` and declare a parameter with the same name in
the handler signature.

## Texts

Every user-visible string goes through `t("key")` in `core/messages.py`, and
every key must exist in **both** the `en` and `ru` dictionaries. Tool-call
labels streamed during a turn (e.g. `mcp__bot__send_document` → "📎 Sending
document...") are mapped in `core/services/cc_events.py`.

## Sending files to Telegram from an agent session

`mcp-servers/bot/server.py` exposes `send_message`, `send_document` (any file
≤ 50 MB — Bot API upload limit; APK/ZIP/etc. all fine) and `send_image`
(≤ 10 MB rendered inline, otherwise falls back to a document).

Routing is injected per session: `bot_mcp_runtime.ensure_bot_runtime_mcp_config`
writes `tmux_sessions/<name>/mcp.runtime.json` with `TELEGRAM_CHAT_ID`,
`TELEGRAM_THREAD_ID` and `TELEGRAM_CONTEXT_LOCK=1`, and the engine is launched
with `--mcp-config <that file> --strict-mcp-config`. Consequences:

- Only **bot-spawned** sessions have these tools. A plain terminal session in
  the same repo does not, and cannot send to Telegram by itself.
- `TELEGRAM_CONTEXT_LOCK=1` pins sends to the session's own chat/topic;
  explicit mismatching `chat_id`/`thread_id` arguments are rejected.
- **Managed CLI builds get no MCP at all**: `claude_mcp_config_flags` in
  `core/services/providers.py` skips `--mcp-config` for `tcloud`/`tclaude`,
  because their signed MCP allowlist blocks the `bot` server at startup and
  the session hangs on the boot prompt. On a machine where the managed build
  is installed (it is preferred by `resolve_claude_binary`), no bot-spawned
  Claude session has `send_document`.
- The policy-proof alternative is `/sendfile <absolute path> [caption]`
  (`handle_sendfile_command` in `core/handlers/commands.py`): the **bot
  process** uploads the file itself, independent of engine and MCP policy.
  An agent that built an artifact only needs to report its absolute path.

New MCP tools follow the same shape: `@mcp.tool()` in `server.py`, resolve
routing via `_resolve_routing`, return human-readable error strings instead of
raising. Add a streaming label in `cc_events.py` and both locales in
`messages.py`.

## Tests and checks

`tests/test_public_runtime.py` pins public behavior; notable couplings:

- `test_model_presets_are_valid_overrides` runs every `Settings.model_presets`
  entry through `normalize_model_override` — new presets must stay round-trip
  clean.
- Reply-keyboard button texts are contract surface for handlers registered on
  `F.text == t("ui.btn_*")`.

Before handoff:

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run mypy src/ mcp-servers/bot/server.py && uv run pytest
```

## Public-repo rules (short form)

No real chat/user IDs, tokens, machine-local paths, or runtime state in
commits. Runtime files (`topic_config.json`, `session_mapping.json`,
`channel_sessions.json`, `tmux_sessions/`, `.env`, `.mcp*.json`, `data/`)
stay untracked. See
`.claude/skills/project-knowledge/references/release-safety.md`.
