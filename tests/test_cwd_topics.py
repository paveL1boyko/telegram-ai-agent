"""Tests for /cwd session restart, folder picker, and topic auto-naming."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.core.config import Settings
from telegram_bot.core.handlers.commands import _apply_cwd_override
from telegram_bot.core.keyboards import cwd_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services.cwd_picker import CwdPickerState, CwdPickerStore, list_subdirs
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_naming import (
    abbreviate_folder,
    model_emoji,
    set_topic_icons,
    short_model_name,
    sync_all_topic_names,
    sync_topic_name,
    topic_display_name,
    topic_icon_emoji,
    topic_icon_id,
)
from telegram_bot.core.services.topic_runtime import BotDefaults


def test_short_model_name_compacts_claude_family() -> None:
    assert short_model_name(None) is None
    assert short_model_name("") is None
    assert short_model_name("anthropic/claude-fable-5[1m]") == "fable"
    assert short_model_name("anthropic/claude-opus-5[1m]") == "opus"
    assert short_model_name("claude-sonnet-4-5") == "sonnet"
    assert short_model_name("claude-haiku-4-5-20251001") == "haiku"


def test_short_model_name_keeps_vendor_tails() -> None:
    assert short_model_name("openrouter/moonshotai/kimi-k3[1m]") == "kimi-k3"
    assert short_model_name("openrouter/openai/gpt-5.5[1m]") == "gpt-5.5"
    assert short_model_name("openrouter/x-ai/grok-4.6[1m]") == "grok-4.6"
    assert short_model_name("selectel/z-ai/glm-5.3") == "glm-5.3"


def test_abbreviate_folder() -> None:
    assert abbreviate_folder("telegram-ai-agent") == "Te.Ai.Ag"
    assert abbreviate_folder("ecom_merchants_partners") == "Ec.Me.Pa"
    assert abbreviate_folder("xpath-generator") == "Xp.Ge"
    assert abbreviate_folder("ceneo") == "Ce"
    assert abbreviate_folder("xpathGenerator") == "Xp.Ge"
    assert abbreviate_folder("my.project v2") == "My.Pr.V2"
    assert abbreviate_folder("мой_проект") == "Мо.Пр"  # noqa: RUF001
    # Nothing to abbreviate — returned as-is.
    assert abbreviate_folder("---") == "---"


def test_topic_display_name() -> None:
    assert topic_display_name("/home/u/xpath-generator", None) == "Xp.Ge"
    assert (
        topic_display_name("/home/u/xpath-generator", "anthropic/claude-fable-5[1m]")
        == "Xp.Ge · fable"
    )
    # Emoji without a matching topic icon is inlined as a prefix.
    assert topic_display_name("/home/u/xpath-generator", "opus", "🎯") == "🎯 Xp.Ge · opus"
    # Root cwd has no folder name — fall back to the raw path.
    assert topic_display_name("/", None) == "/"
    # Telegram caps topic names at 128 chars.
    long_name = topic_display_name(f"/x/{'a' * 200}", "opus")
    assert len(long_name) <= 128


def test_topic_icon_lookup_ignores_variation_selector() -> None:
    set_topic_icons({"⚡️": "111", "🤖": "222"})
    try:
        assert topic_icon_id("⚡") == "111"
        assert topic_icon_id("⚡️") == "111"
        assert topic_icon_id("🎯") is None
        assert topic_icon_id(None) is None
        assert topic_icon_emoji("222") == "🤖"
        assert topic_icon_emoji("999") is None
    finally:
        set_topic_icons({})


def test_model_emoji_follows_model_family() -> None:
    assert model_emoji("anthropic/claude-opus-5[1m]") == "🧠"
    assert model_emoji("anthropic/claude-fable-5[1m]") == "🔮"
    assert model_emoji("openrouter/openai/gpt-5.5[1m]") == "🔥"
    assert model_emoji("openrouter/moonshotai/kimi-k3[1m]") == "💎"
    assert model_emoji("openrouter/x-ai/grok-4.6[1m]") == "⚡️"
    assert model_emoji("selectel/z-ai/glm-5.3") == "🧪"
    # Unknown vendor → neutral default; no override → engine decides.
    assert model_emoji("openrouter/acme/wizard-9") == "💻"
    assert model_emoji(None, "claude") == "🔮"
    assert model_emoji(None, "codex") == "🤖"
    # Distinct models never share an icon among the presets the bot offers.
    presets = Settings(telegram_bot_token="test").model_presets
    assert len({model_emoji(m) for m in presets}) == len(presets)


def test_list_subdirs_skips_hidden_and_files(tmp_path: Path) -> None:
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    names = [p.name for p in list_subdirs(tmp_path)]
    assert names == ["Alpha", "beta"]


def test_list_subdirs_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert list_subdirs(tmp_path / "nope") == []


def test_cwd_keyboard_pagination_and_marker(tmp_path: Path) -> None:
    dirs = [tmp_path / f"proj{i:02d}" for i in range(10)]
    kb = cwd_keyboard(
        dirs,
        page=0,
        list_dir=tmp_path,
        current_cwd=tmp_path / "proj02",
        token="abcd1234",
    )
    rows = kb.inline_keyboard
    # 8 dir rows + pagination row + nav row + cancel row
    assert len(rows) == 8 + 3
    marked = [b.text for r in rows[:8] for b in r if b.text.startswith("✅ 📁")]
    assert marked == ["✅ 📁 proj02"]
    callbacks = [b.callback_data for r in rows[:8] for b in r]
    assert callbacks[0] == "cw:s:abcd1234:0"
    assert callbacks[-1] == "cw:s:abcd1234:7"
    page_row = rows[8]
    assert page_row[1].text == "1/2"
    assert page_row[1].callback_data == "cw:p:abcd1234:0"
    assert all(len(b.callback_data.encode()) <= 64 for r in rows for b in r if b.callback_data)


def test_cwd_keyboard_single_page_has_no_pager(tmp_path: Path) -> None:
    kb = cwd_keyboard([tmp_path / "a"], page=0, list_dir=tmp_path, current_cwd=None, token="t")
    texts = [b.text for r in kb.inline_keyboard for b in r]
    assert "1/1" not in texts
    assert any(t.startswith("✅ ") and not t.startswith("✅ 📁") for t in texts)
    assert "⬆ .." in texts
    assert "✕" in texts


def test_cwd_picker_store_move_up_and_ttl(tmp_path: Path) -> None:
    clock_now = 1000.0
    store = CwdPickerStore(ttl_sec=10.0, clock=lambda: clock_now)
    child = tmp_path / "a" / "b"
    token = store.put(CwdPickerState(chat_id=1, thread_id=2, list_dir=child, created_at=clock_now))
    moved = store.move_up(token)
    assert moved is not None and moved.list_dir == child.parent
    again = store.move_up(token)
    assert again is not None and again.list_dir == child.parent.parent
    # TTL expiry on access
    clock_now += 11.0
    assert store.get(token) is None
    assert store.move_up(token) is None


def _make_topic_config(tmp_path: Path, *, exec_mode: str, model: str | None) -> TopicConfig:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    "42": {
                        "name": "old-name",
                        "type": "project",
                        "mode": "free",
                        "cwd": None,
                        "exec_mode": exec_mode,
                        "engine": "claude",
                        "model": model,
                    }
                }
            }
        )
    )
    return TopicConfig(str(config_path))


class _FakeBot:
    def __init__(self) -> None:
        self.renames: list[tuple[int, int, str]] = []
        self.icons: list[str | None] = []

    async def edit_forum_topic(
        self,
        *,
        chat_id: int,
        message_thread_id: int,
        name: str,
        icon_custom_emoji_id: str | None = None,
    ) -> None:
        self.renames.append((chat_id, message_thread_id, name))
        self.icons.append(icon_custom_emoji_id)


class _TmuxStub:
    def __init__(self) -> None:
        self.killed: list[object] = []
        self.started: list[dict[str, object]] = []

    async def kill(self, key: object) -> None:
        self.killed.append(key)

    async def start_session(self, key: object, **kwargs: object) -> None:
        self.started.append(kwargs)


class _SessionManagerStub:
    def __init__(self, cwd: str) -> None:
        self.killed: list[object] = []
        self._cwd = cwd

    async def kill_session(self, key: object) -> None:
        self.killed.append(key)

    def _get_session(self, key: object) -> SimpleNamespace:
        return SimpleNamespace(
            mode="free",
            cwd=self._cwd,
            mcp_config="",
            chat_id=1,
            engine="claude",
            model=None,
        )


class _QueueStub:
    def __init__(self) -> None:
        self.cleared = 0

    async def clear(self, key: object) -> None:
        self.cleared += 1


class _BatcherStub:
    def __init__(self) -> None:
        self.cleared = 0

    def clear(self, key: object) -> None:
        self.cleared += 1


async def test_apply_cwd_override_restarts_tmux_and_renames(tmp_path: Path) -> None:
    work = tmp_path / "xpath-generator"
    work.mkdir()
    topic_config = _make_topic_config(tmp_path, exec_mode="tmux", model=None)
    bot = _FakeBot()
    tmux = _TmuxStub()
    session_manager = _SessionManagerStub(str(work))
    settings = Settings(telegram_bot_token="test")
    defaults = BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json")

    text = await _apply_cwd_override(
        chat_id=100,
        thread_id=42,
        new_cwd=str(work),
        bot=bot,  # type: ignore[arg-type]
        settings=settings,
        topic_config=topic_config,
        bot_defaults=defaults,
        session_manager=session_manager,  # type: ignore[arg-type]
        tmux_manager=tmux,  # type: ignore[arg-type]
        message_queue=_QueueStub(),  # type: ignore[arg-type]
        forward_batcher=_BatcherStub(),  # type: ignore[arg-type]
    )

    assert topic_config.get_topic(42).cwd == str(work)
    assert tmux.killed == [(100, 42)]
    assert len(tmux.started) == 1
    assert tmux.started[0]["cwd"] == str(work)
    assert session_manager.killed == [(100, 42)]
    # No icon catalog in tests → the model emoji (claude default → fable) is inlined.
    assert bot.renames == [(100, 42, "🔮 Xp.Ge")]
    assert text == t("ui.cwd_set_restarted", cwd=str(work))


async def test_apply_cwd_override_subprocess_kills_session_without_respawn(
    tmp_path: Path,
) -> None:
    work = tmp_path / "proj"
    work.mkdir()
    topic_config = _make_topic_config(tmp_path, exec_mode="subprocess", model=None)
    bot = _FakeBot()
    tmux = _TmuxStub()
    session_manager = _SessionManagerStub(str(work))
    settings = Settings(telegram_bot_token="test")
    defaults = BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json")

    text = await _apply_cwd_override(
        chat_id=100,
        thread_id=42,
        new_cwd=str(work),
        bot=bot,  # type: ignore[arg-type]
        settings=settings,
        topic_config=topic_config,
        bot_defaults=defaults,
        session_manager=session_manager,  # type: ignore[arg-type]
        tmux_manager=tmux,  # type: ignore[arg-type]
        message_queue=_QueueStub(),  # type: ignore[arg-type]
        forward_batcher=_BatcherStub(),  # type: ignore[arg-type]
    )

    assert session_manager.killed == [(100, 42)]
    assert tmux.killed == [] and tmux.started == []
    assert text == t("ui.cwd_set_next", cwd=str(work))


async def test_sync_topic_name_includes_short_model_suffix(tmp_path: Path) -> None:
    topic_config = _make_topic_config(
        tmp_path, exec_mode="tmux", model="anthropic/claude-fable-5[1m]"
    )
    bot = _FakeBot()
    defaults = BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json")

    await sync_topic_name(
        bot,  # type: ignore[arg-type]
        chat_id=100,
        thread_id=42,
        topic_config=topic_config,
        defaults=defaults,
    )
    expected = f"🔮 {abbreviate_folder(tmp_path.name)} · fable"
    assert bot.renames == [(100, 42, expected)]

    # Second call with an unchanged name is a no-op: update the recorded
    # name in config the way the forum_topic_edited handler would.
    config_path = tmp_path / "topic_config.json"
    data = json.loads(config_path.read_text())
    data["topics"]["42"]["name"] = expected
    config_path.write_text(json.dumps(data))
    await sync_topic_name(
        bot,  # type: ignore[arg-type]
        chat_id=100,
        thread_id=42,
        topic_config=topic_config,
        defaults=defaults,
    )
    assert len(bot.renames) == 1

    # force=True re-applies name + icon even when the name is unchanged.
    await sync_topic_name(
        bot,  # type: ignore[arg-type]
        chat_id=100,
        thread_id=42,
        topic_config=topic_config,
        defaults=defaults,
        force=True,
    )
    assert len(bot.renames) == 2


async def test_sync_all_topic_names_applies_emoji_icon(tmp_path: Path) -> None:
    work = tmp_path / "ceneo"
    work.mkdir()
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps(
            {
                "topics": {
                    # Explicit emoji override wins over the model mapping.
                    "42": {"name": "old", "type": "project", "cwd": None, "emoji": "🤖"},
                    "43": {"name": "helper", "type": "assistant", "cwd": None},
                    # No `type` (parses as assistant) but has its own cwd —
                    # the 2026-10-30 `ceneo_pl gpt` entry that was never
                    # renamed. Icon follows the model.
                    "44": {
                        "name": "ceneo_pl gpt",
                        "cwd": str(work),
                        "model": "openrouter/openai/gpt-5.5[1m]",
                    },
                }
            }
        )
    )
    topic_config = TopicConfig(str(config_path))
    bot = _FakeBot()
    defaults = BotDefaults(cwd=tmp_path / "telegram-ai-agent", mcp_config=tmp_path / "mcp.json")
    set_topic_icons({"🤖": "robot-id", "🔥": "fire-id"})
    try:
        renamed = await sync_all_topic_names(
            bot,  # type: ignore[arg-type]
            channel_keys=[(100, 42), (100, 43), (100, 44), (100, None)],
            topic_config=topic_config,
            defaults=defaults,
        )
    finally:
        set_topic_icons({})
    # Assistant topic and the General (None) pseudo-topic are skipped.
    assert renamed == 2
    assert bot.renames == [(100, 42, "Te.Ai.Ag"), (100, 44, "Ce · gpt-5.5")]
    assert bot.icons == ["robot-id", "fire-id"]


async def test_sync_topic_name_inlines_emoji_without_icon(tmp_path: Path) -> None:
    topic_config = _make_topic_config(
        tmp_path, exec_mode="tmux", model="anthropic/claude-opus-5[1m]"
    )
    bot = _FakeBot()
    defaults = BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json")
    # Icon catalog unknown (fetch failed) → the model emoji becomes a name prefix.
    set_topic_icons({})
    await sync_topic_name(
        bot,  # type: ignore[arg-type]
        chat_id=100,
        thread_id=42,
        topic_config=topic_config,
        defaults=defaults,
    )
    assert bot.renames == [(100, 42, f"🧠 {abbreviate_folder(tmp_path.name)} · opus")]
    assert bot.icons == [None]


async def test_sync_topic_name_skips_assistant_topics(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    config_path.write_text(
        json.dumps({"topics": {"42": {"name": "helper", "type": "assistant", "cwd": None}}})
    )
    topic_config = TopicConfig(str(config_path))
    bot = _FakeBot()
    defaults = BotDefaults(cwd=tmp_path, mcp_config=tmp_path / "mcp.json")

    await sync_topic_name(
        bot,  # type: ignore[arg-type]
        chat_id=100,
        thread_id=42,
        topic_config=topic_config,
        defaults=defaults,
    )
    assert bot.renames == []
