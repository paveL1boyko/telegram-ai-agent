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
| File mirror (folder → topic sync) | `src/telegram_bot/core/services/file_sync.py` |
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
5. Picker lifecycle: every settings picker (`/model`, `/engine`, `/uimode`,
   `/stream`) deletes itself after a successful choice via `_close_picker`
   (delete, fall back to stripping the keyboard when deletion fails, e.g.
   the message is older than 48h). Dead pickers must not pile up in the
   topic history — keep this behavior for new pickers.

New handler dependencies (services) are injected by name: add the instance to
`dp["<name>"]` in `__main__.py` and declare a parameter with the same name in
the handler signature.

## `/cwd`, the folder picker, and topic auto-naming

`/cwd <absolute path>` (and `/cwd default`) is one atomic operation in
`_apply_cwd_override`: persist the override via `topic_config.update_cwd`,
then kill the live session and respawn it in the new directory (tmux mode),
or drop the stale subprocess session id (subprocess mode). A live TUI keeps
its spawn-time directory regardless of config, so never persist a cwd change
without the restart — that was the 2026-10-26 bug where "New chat" came back
up in the old directory.

Bare `/cwd` posts a folder picker: subdirectories of the effective cwd's
parent (`services/cwd_picker.py` lists them, `cwd_keyboard` renders pages of
8, `⬆ ..` climbs a level, `✅ <dirname>` selects the listed directory).
Picker state lives in `CwdPickerStore` (token in callback_data, TTL 15 min),
mirroring `PickerStore` for `/resume`.

When `Settings.topic_autoname` is on (default; `TOPIC_AUTONAME=false` to
disable), `/cwd` changes and model/engine switches rename the forum topic to
`<cwd abbrev> · <short model>` via `sync_topic_name`
(`services/topic_naming.py`). The cwd folder is abbreviated to the first two
letters of each word (`telegram-ai-agent` → `Te.Ai.Ag`,
`ecom_merchants_partners` → `Ec.Me.Pa`) so the topic list stays readable on
a phone. Short model names collapse the Claude family
(`anthropic/claude-fable-5[1m]` → `fable`) and keep other vendors' tails
(`kimi-k3`, `gpt-5.5`). Renames are best-effort — the bot needs the
`manage_topics` admin right, failures are logged and swallowed. Assistant
topics (no cwd concept) are never renamed.

The topic icon follows the model (`model_emoji` in `topic_naming.py`: opus 🧠,
fable 🔮, gpt 🔥, kimi 💎, grok ⚡️, glm 🧪, …, chosen for distinct colours
so topics are telling apart at sidebar size), so a model switch re-badges
the topic along with the name. A per-topic `emoji` in `topic_config.json`
pins a different icon (`null` = follow the model). When the emoji is one of
Telegram's forum icon emoji (`getForumTopicIconStickers`, ~110 entries,
fetched once at startup) it becomes the topic icon; otherwise it is inlined
as a name prefix. An icon picked by a human in the Telegram UI is mirrored
into `emoji` by the `forum_topic_edited` handler (the bot's own edits echo
there too and are deliberately not persisted, so the icon keeps following
the model). Existing topics are brought onto the current scheme by a startup
pass over restored tmux sessions (`sync_all_topic_names`) and, for topics
without a session, once per process on their first text message
(`sync_topic_name_once`). A config entry without `type` parses as
`assistant`; it is still autonamed when it carries its own `cwd`.

Pinning topics in a fixed order is not possible through the Bot API — there is
no pin/reorder method for forum topics (only `pinChatMessage` for messages);
"Pin topic" exists solely in the Telegram client UI for group admins.

## File mirror: `/filesdir` and `/files`

A topic can mirror on-disk folders: every matching file is uploaded to the
topic once, re-uploaded when it changes, and its message deleted when the
file disappears (documents cannot be edited via the Bot API, so "update" is
always send-new-then-delete-old — never the reverse, a failed send loses
nothing). Run the commands **in the target topic**; the mirrored folder can
belong to any other topic's session cwd.

- `/filesdir <absolute path> [glob]` — add a mirror entry and upload
  everything matching immediately. The glob is a path pattern relative to
  the folder (`*.md` flat, `**/*.pdf` recursive, default `**/*`); a trailing
  token counts as a glob only when it has wildcard chars and the head is an
  existing directory, so paths with spaces survive. Entries with more than
  `200` matching files are refused up front.
- `/filesdir` — list the topic's entries; `/filesdir rm <n>` removes one;
  `/filesdir clear` disables the mirror.
- `/files` — sync right now, without the stability wait.

Persistence: entries live in `topic_config.json` (`files_dirs`, a list of
`{"dir", "pattern"}` objects — a bare string is shorthand for the default
pattern); per-topic message-id state lives in `file_sync_state/<thread>.json`,
which also records the chat id — the watchdog only knows thread ids, so a
mirror added by hand-editing the config posts nothing until `/files` runs
in the topic once. The watchdog ticks every `FILE_SYNC_INTERVAL_SEC` (30s;
`0` disables it — `/files` keeps working) and uploads a file only after two
consecutive ticks see the same `(mtime, size)`, so an agent mid-write never
ships a torn read.

## Typing directly into the tmux pane

A transcript tail only exists while a Telegram-originated turn runs. Output
produced by typing straight into the tmux pane is picked up by the
**activity watchdog** (`TmuxManager.start_activity_watchdog`, 10s tick):
when a session's transcript grows beyond the delivered offset and no tail is
running, it starts a recovery tail that streams the events live. Delivery
goes through `build_recovery_on_event_factory`
(`core/handlers/streaming.py`), wired at startup via
`tmux_manager.wire_recovery_events(...)` — text/result events become regular
messages, status follows the topic's stream_mode, message ids are recorded
for reply-to-resume. `resume_tails` (tail-per-session at boot) is
deliberately NOT revived; the watchdog starts tails on demand only.

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
