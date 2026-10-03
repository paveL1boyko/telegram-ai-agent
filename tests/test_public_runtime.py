from __future__ import annotations

import importlib.util
import json
from datetime import UTC
from pathlib import Path

from aiogram.filters import Command, CommandObject

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers import commands
from telegram_bot.core.handlers.tail import handle_tail_command
from telegram_bot.core.keyboards import model_keyboard
from telegram_bot.core.services import cc_modes
from telegram_bot.core.services.bot_commands import build_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.providers import (
    CLAUDE_ADAPTER,
    CODEX_ADAPTER,
    NESSY_ADAPTER,
    choose_available_engine,
)
from telegram_bot.core.services.tmux_spawn import topic_slug
from telegram_bot.core.services.topic_config import TopicConfig, normalize_model_override
from telegram_bot.core.tui.paths import generate_session_uuid
from telegram_bot.core.tui.routing import BOT_RESERVED_COMMANDS, route_slash_command


def test_public_settings_default_cwd_is_generic(monkeypatch) -> None:
    for name in ("BOT_LANG", "PROJECT_ROOT", "DEFAULT_CWD", "TOPIC_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, telegram_bot_token="test-token")

    assert settings.bot_lang == "en"
    assert settings.project_root == "."
    assert settings.default_cwd == "."
    assert settings.topic_config_path == "./topic_config.json"


def test_public_relative_file_cache_dir_is_agent_readable(tmp_path: Path) -> None:
    root = tmp_path / "bot"
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        project_root=str(root),
        default_cwd=".",
        file_cache_dir="./data",
    )
    session_manager = SessionManager(settings)

    assert session_manager.file_cache_dir == str((root / "data").resolve())


def test_public_start_wires_live_buffer_before_restore_all() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "tmux_manager.wire_live_buffer(bot=bot, topic_config=topic_config)" in source
    assert source.index("tmux_manager.wire_live_buffer") < source.index("tmux_manager.restore_all")


def test_public_prompt_modes_are_available() -> None:
    prompts_dir = Path("src/telegram_bot/prompts")
    assert {path.name for path in prompts_dir.glob("*.md")} == {
        "default.md",
        "task-manager.md",
    }
    for mode in ("free", "task"):
        assert cc_modes._get_mode_prompt(mode)
    assert cc_modes._get_mode_prompt("task") == (prompts_dir / "task-manager.md").read_text()


def test_public_prompt_modes_have_bot_mcp_tools() -> None:
    required = {
        "mcp__bot__send_message",
        "mcp__bot__send_image",
        "mcp__bot__send_document",
    }

    for mode in ("free", "task"):
        tools = set(cc_modes._MODE_TOOLS[mode].split(","))
        assert required <= tools
        assert "mcp__bot__send_file" not in tools


def test_free_mode_allows_skill_for_topic_setup() -> None:
    tools = set(cc_modes._MODE_TOOLS["free"].split(","))

    assert "Skill" in tools


def test_engine_selection_falls_back_to_available_cli(monkeypatch) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.services.providers.is_engine_available",
        lambda engine: engine == "codex",
    )

    assert choose_available_engine("claude") == "codex"


def test_topic_config_parses_public_runtime_fields(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    mcp_config = project / ".mcp.json"
    mcp_config.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "name": "Demo",
                        "type": "project",
                        "mode": "free",
                        "cwd": str(project),
                        "mcp_config": str(mcp_config),
                        "stream_mode": "minimal",
                        "exec_mode": "tmux",
                        "engine": "codex",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    topic = TopicConfig(str(config_path)).get_topic(42)

    assert topic.name == "Demo"
    assert topic.mode == "free"
    assert topic.cwd == str(project)
    assert topic.mcp_config == str(mcp_config)
    assert topic.stream_mode == "minimal"
    assert topic.exec_mode == "tmux"
    assert topic.engine == "codex"


def test_codex_provider_parser_smoke() -> None:
    parsed = CODEX_ADAPTER.parse_exec_event('{"type":"thread.started","thread_id":"abc"}')

    assert parsed.session_id == "abc"
    assert parsed.events == []


def test_model_override_keeps_routing_prefix_and_context_suffix() -> None:
    # `[1m]` selects the 1M context window and `openrouter/...` selects the
    # route; dropping either silently downgrades the session.
    assert normalize_model_override("anthropic/claude-fable-5[1m]") == (
        "anthropic/claude-fable-5[1m]"
    )
    assert normalize_model_override("openrouter/moonshotai/kimi-k3[1m]") == (
        "openrouter/moonshotai/kimi-k3[1m]"
    )
    # Dotted ids (gpt-5.5, grok-4.6) must survive verbatim too.
    assert normalize_model_override("openrouter/openai/gpt-5.5[1m]") == (
        "openrouter/openai/gpt-5.5[1m]"
    )
    assert normalize_model_override("x-ai/grok-4.6[1m]") == "x-ai/grok-4.6[1m]"
    assert normalize_model_override("openai/gpt-6-astra[1m]") == "openai/gpt-6-astra[1m]"
    assert normalize_model_override("opus[1m]") == "opus[1m]"
    assert normalize_model_override("claude-fable-5") == "claude-fable-5"
    assert normalize_model_override("  claude-fable-5  ") == "claude-fable-5"
    assert normalize_model_override("") is None
    assert normalize_model_override(None) is None
    # A value that could be read as a CLI flag must never reach argv.
    assert normalize_model_override("--dangerously-skip-permissions") is None
    assert normalize_model_override("opus; rm -rf /") is None


def test_claude_tmux_args_pin_model_on_start_and_resume(tmp_path: Path) -> None:
    # `claude --resume` otherwise adopts the transcript's model, which never
    # carries the `[1m]` suffix — the resumed session comes back at the
    # default context window.
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        project_root=str(tmp_path),
    )
    session_manager = SessionManager(settings)
    model = "anthropic/claude-fable-5[1m]"

    started = session_manager.build_tmux_startup_args(
        session_id_new=generate_session_uuid(),
        model=model,
    )
    resumed = session_manager.build_tmux_startup_args(
        resume_session_id=generate_session_uuid(),
        model=model,
    )
    default = session_manager.build_tmux_startup_args(
        resume_session_id=generate_session_uuid(),
    )

    for argv in (started, resumed):
        assert argv[argv.index("--model") + 1] == model
    assert "--model" not in default


def test_claude_adapter_pins_model_on_start_and_resume() -> None:
    model = "openrouter/qwen/qwen3.8-max[1m]"

    started = CLAUDE_ADAPTER.build_tui_start(cwd=".", model=model)
    resumed = CLAUDE_ADAPTER.build_tui_resume(
        cwd=".",
        session_id=generate_session_uuid(),
        model=model,
    )

    for argv in (started, resumed):
        assert argv[argv.index("--model") + 1] == model
    assert "--model" not in CLAUDE_ADAPTER.build_tui_start(cwd=".")


def test_model_presets_are_valid_overrides() -> None:
    settings = Settings(_env_file=None, telegram_bot_token="test-token")
    expected = [
        "opus[1m]",
        "anthropic/claude-opus-4-7[1m]",
        "openrouter/anthropic/claude-opus-4.7[1m]",
        "anthropic/claude-opus-5[1m]",
        "openrouter/anthropic/claude-opus-5[1m]",
        "anthropic/claude-fable-5[1m]",
        "openrouter/anthropic/claude-fable-5[1m]",
        "openrouter/qwen/qwen3.8-max[1m]",
        "openrouter/moonshotai/kimi-k3[1m]",
        "openrouter/x-ai/grok-4.6[1m]",
        "x-ai/grok-4.6[1m]",
        "openrouter/openai/gpt-5.5[1m]",
        "openai/gpt-5.5[1m]",
        "openrouter/openai/gpt-6-astra[1m]",
        "openai/gpt-6-astra[1m]",
    ]

    assert settings.model_presets == expected
    for preset in settings.model_presets:
        assert normalize_model_override(preset) == preset


def test_model_keyboard_offers_default_reset_and_fits_callback_limit() -> None:
    presets = ["openrouter/anthropic/claude-fable-5[1m]", "anthropic/claude-fable-5"]

    markup = model_keyboard(presets, current_model=presets[1])
    buttons = [button for row in markup.inline_keyboard for button in row]
    payloads = [button.callback_data for button in buttons]

    assert payloads == ["model:default", "model:auto", "model:0", "model:1"]
    # Telegram rejects callback_data over 64 bytes; model ids are too long
    # to embed, hence the index indirection.
    for payload in payloads:
        assert payload is not None
        assert len(payload.encode()) <= 64
    assert buttons[3].text.startswith("✅")


def test_model_keyboard_marks_the_auto_row_when_the_topic_is_in_rotation() -> None:
    presets = ["anthropic/claude-fable-5"]

    markup = model_keyboard(presets, current_model="auto")
    buttons = [button for row in markup.inline_keyboard for button in row]

    # `auto` is not a preset, so no preset row may claim the checkmark
    assert buttons[1].callback_data == "model:auto"
    assert buttons[1].text.startswith("✅")
    assert not buttons[0].text.startswith("✅")
    assert not buttons[2].text.startswith("✅")


def test_live_model_switch_commands() -> None:
    # Claude and Nessy accept `/model <id>` inside a running session, so the
    # bot can switch models without losing context. The `[1m]` suffix must
    # survive — it selects the 1M context window.
    assert CLAUDE_ADAPTER.live_model_command("anthropic/claude-fable-5[1m]") == (
        "/model anthropic/claude-fable-5[1m]"
    )
    assert CLAUDE_ADAPTER.live_model_command(None) == "/model default"
    assert NESSY_ADAPTER.live_model_command("tgpt/qwen") == "/model tgpt/qwen"
    assert NESSY_ADAPTER.live_model_command(None) == "/model default"
    # Codex honours --model at launch only — the caller must respawn instead.
    assert CODEX_ADAPTER.live_model_command("gpt-5") is None
    assert CODEX_ADAPTER.live_model_command(None) is None


def test_compact_slash_commands() -> None:
    assert CLAUDE_ADAPTER.compact_slash_command() == "/compact"
    assert CODEX_ADAPTER.compact_slash_command() == "/compact"
    # Gemini-style CLI compresses context via /compress.
    assert NESSY_ADAPTER.compact_slash_command() == "/compress"


def test_compact_command_is_bot_handled_and_reserved() -> None:
    # A dedicated handler delivers /compact to the live tmux session, and the
    # reservation keeps the text passthrough from also forwarding it raw.
    assert commands.handle_compact_command is not None
    assert "/compact" in BOT_RESERVED_COMMANDS
    assert route_slash_command("/compact") == "bot"


def test_public_bot_command_menu_includes_compact() -> None:
    command_names = {command.command for command in build_bot_commands("ru")}
    assert "compact" in command_names


def test_update_session_model_records_live_switch(tmp_path: Path) -> None:
    from telegram_bot.core.services.tmux_manager import TmuxManager
    from telegram_bot.core.services.tmux_state import TmuxSessionState

    manager = TmuxManager(sessions_dir=tmp_path)
    key = (1, 2)
    state = TmuxSessionState(
        session_name="cc-1-2",
        session_dir=str(tmp_path / "cc-1-2"),
        session_id="session-abc",
        mode="free",
        cwd=".",
        mcp_config="",
        chat_id=1,
    )
    manager._sessions[key] = state

    assert manager.update_session_model(key, "anthropic/claude-fable-5[1m]") is True
    assert state.model == "anthropic/claude-fable-5[1m]"
    # Unknown channel — nothing to update.
    assert manager.update_session_model((9, 9), "x") is False


def test_send_direct_set_processing_controls_busy_flag(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from telegram_bot.core.services.tmux_manager import TmuxManager
    from telegram_bot.core.services.tmux_state import TmuxSessionState

    manager = TmuxManager(sessions_dir=tmp_path)
    key = (1, 2)
    state = TmuxSessionState(
        session_name="cc-1-2",
        session_dir=str(tmp_path / "cc-1-2"),
        session_id="session-abc",
        mode="free",
        cwd=".",
        mcp_config="",
        chat_id=1,
    )
    manager._sessions[key] = state

    async def fake_safe_send(channel_key, st, prompt):
        return True

    monkeypatch.setattr(manager, "_safe_send_and_enter", fake_safe_send)

    async def scenario() -> None:
        # A control command (live /model switch) must not wedge the busy flag.
        assert await manager.send_direct(key, "/model sonnet", set_processing=False) is True
        assert manager.is_processing(key) is False
        # A normal prompt still marks the channel as processing.
        assert await manager.send_direct(key, "hello") is True
        assert manager.is_processing(key) is True

    asyncio.run(scenario())


def test_topic_slug_is_snake_case_and_bounded() -> None:
    assert topic_slug("Ecom Merchants — Partners!") == "ecom_merchants_partners"
    assert topic_slug("  Мой Топик / 2  ") == "мой_топик_2"
    assert topic_slug("a" * 80) == "a" * 40
    assert topic_slug("---") == ""
    assert topic_slug(None) == ""


def test_bare_model_command_is_bot_handled_and_model_with_args_reaches_tui() -> None:
    # `/model` opens the bot's picker, but `/model sonnet` must still fall
    # through to the CLI's own dialog in the tmux pane.
    handler = next(
        registered
        for registered in commands.router.message.handlers
        if registered.callback is commands.handle_model_command
    )
    command_filter = next(
        wrapped.callback for wrapped in handler.filters if isinstance(wrapped.callback, Command)
    )

    assert command_filter.magic is not None
    assert command_filter.magic.resolve(CommandObject(prefix="/", command="model", args=None))
    assert not command_filter.magic.resolve(
        CommandObject(prefix="/", command="model", args="sonnet")
    )
    # Listing it in BOT_RESERVED_COMMANDS would swallow the argument form,
    # which has no bot handler at all.
    assert "/model" not in BOT_RESERVED_COMMANDS
    assert route_slash_command("/model sonnet") == "tui"


def test_public_command_handlers_are_wired() -> None:
    assert commands.handle_resume is not None
    assert commands.handle_stream_mode is not None
    assert commands.handle_uimode_command is not None
    assert commands.handle_engine_command is not None
    assert commands.handle_model_command is not None
    assert commands.on_model_click is not None
    assert commands.handle_sendfile_command is not None
    assert handle_tail_command is not None


def test_sendfile_validation(tmp_path) -> None:
    error = commands._sendfile_error("relative/path.apk")
    assert error is not None and error[0] == "ui.sendfile_not_absolute"

    error = commands._sendfile_error(str(tmp_path / "missing.apk"))
    assert error is not None and error[0] == "ui.sendfile_not_found"

    artifact = tmp_path / "app.apk"
    artifact.write_bytes(b"apk")
    assert commands._sendfile_error(str(artifact)) is None

    # Whole-string path wins when the file exists, even with spaces.
    spaced = tmp_path / "my build.apk"
    spaced.write_bytes(b"apk")
    assert commands._sendfile_split_args(str(spaced)) == (str(spaced), "")
    path, caption = commands._sendfile_split_args(f"{artifact} release build")
    assert path == str(artifact)
    assert caption == "release build"


def test_public_bot_command_menu_is_public_only() -> None:
    command_names = {command.command for command in build_bot_commands("ru")}

    assert "clear" in command_names
    assert "tui" in command_names
    assert "tail" not in command_names
    assert "new" not in command_names
    assert "day" not in command_names


def test_public_start_registers_bot_commands() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "setup_bot_commands(bot)" in source
    assert source.index("setup_bot_commands(bot)") < source.index("dp.start_polling")


def test_mcp_bot_server_imports() -> None:
    path = Path("mcp-servers/bot/server.py")
    spec = importlib.util.spec_from_file_location("public_bot_mcp_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "send_message")
    assert hasattr(module, "send_image")
    assert hasattr(module, "send_document")
    assert not hasattr(module, "send_file")


async def test_tui_button_always_opens_fresh_panel_and_deletes_stale_in_background(
    tmp_path: Path, monkeypatch
) -> None:
    """Open-first button: press => new panel immediately, stale panel + trigger
    deleted by background tasks (previously the press only closed the stale
    panel and opened nothing — the "button did nothing" complaint)."""
    import asyncio
    import subprocess as real_subprocess
    from datetime import datetime
    from types import SimpleNamespace

    from aiogram.types import Chat, Message

    from telegram_bot.core.handlers import tail as tail_module
    from telegram_bot.core.services.tmux_manager import TmuxManager
    from telegram_bot.core.services.tmux_state import TmuxSessionState

    manager = TmuxManager(sessions_dir=tmp_path)
    key = (1, 2)
    manager._sessions[key] = TmuxSessionState(
        session_name="cc-1-2",
        session_dir=str(tmp_path / "cc-1-2"),
        session_id="session-abc",
        mode="free",
        cwd=".",
        mcp_config="",
        chat_id=1,
    )
    monkeypatch.setattr(manager, "_tmux_alive", lambda name: True)
    # Stale panel registered by an earlier press, far up the history.
    manager.set_tui_panel(key, 111)

    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return SimpleNamespace(stdout="claude > pane snapshot")

    monkeypatch.setattr(
        tail_module,
        "subprocess",
        SimpleNamespace(run=fake_run, CalledProcessError=real_subprocess.CalledProcessError),
    )

    deleted: list[int] = []
    answers: list[tuple[str, dict]] = []

    class FakeBot:
        async def delete_message(self, chat_id: int, message_id: int) -> None:
            deleted.append(message_id)

    sent_panel = Message(
        message_id=222,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="supergroup"),
    )

    class FakeTrigger:
        message_id = 100
        message_thread_id = 2
        chat = SimpleNamespace(id=1)
        bot = FakeBot()

        async def answer(self, text: str, **kwargs):
            answers.append((text, kwargs))
            return sent_panel

    await tail_module.handle_tui_button(FakeTrigger(), manager)
    # Drain the fire-and-forget cleanup tasks.
    await asyncio.gather(*tail_module._background_tasks)

    # New panel captured, sent, and registered — a single press.
    assert captured_cmds and captured_cmds[0][1] == "capture-pane"
    assert len(answers) == 1
    assert "<pre>" in answers[0][0]
    assert answers[0][1].get("reply_markup") is not None
    assert manager._tui_panel_messages[key] == 222
    # Trigger message and the stale panel both deleted in the background;
    # the fresh panel obviously stays.
    assert sorted(deleted) == [100, 111]
