# Claude Instructions

Follow `AGENTS.md`. This is a public generic Telegram bot runtime, not a
personal assistant repository.

Before editing, inspect the relevant files and keep changes scoped. Do not copy
private prompts, private project knowledge, local runtime configs, secrets, or
machine-specific deployment files into this repository.

Use the public project knowledge skill when you need architecture or release
context:

- `.claude/skills/project-knowledge/SKILL.md`

Before adding a command, picker, text, or MCP capability, read
`docs/feature-guide.md` — it maps where each concern lives and the established
handler/keyboard/i18n patterns.

Use `.claude/skills/bot-setup/SKILL.md` for installation, systemd autostart,
language, commands, and operator troubleshooting. Use
`.claude/skills/topic-setup/SKILL.md` for Telegram forum topic wiring.

Run the standard checks before final handoff:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
```

After committing final changes, restart the running bot so it picks up the
new code: `make reload` (bot-only restart — agent `cc-*` tmux sessions keep
running and are reattached on startup via `restore_all`). Note: the sandboxed
shell cannot run `pgrep`, so `make reload` may report "failed to start" even
when the bot started fine — verify with `tmux ls` and `tail .tg-bot.log`
instead of rerunning it.
