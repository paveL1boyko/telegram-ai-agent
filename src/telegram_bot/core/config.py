"""Core configuration loading from environment variables.

Core settings cover generic bot functionality: token, auth, Claude Code,
voice transcription, sessions, tmux, and topics. Downstream projects can layer
their own settings on top of these generic core settings.
"""

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict

# Voice message size cap enforced at every ingestion point (incoming
# voice, forwarded voice, media-content router). Telegram itself caps
# voice files, but an up-front byte check avoids a pointless download
# when a relay or future bot API change lets a large payload through.
MAX_VOICE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    allowed_user_ids: list[int] = []
    bot_lang: str = "en"
    # Directory where handlers download media before forwarding to CC.
    # Core-owned generic bot feature. Override via `FILE_CACHE_DIR` in .env.
    file_cache_dir: str = "/tmp/telegram-bot-cache"
    project_root: str = "."
    default_cwd: str = "."
    session_timeout_sec: int = 86400
    session_cleanup_interval_sec: int = 300
    cc_query_timeout_sec: int = 21600
    deepgram_api_key: str = ""
    cc_wait_timeout_sec: int = 10
    cc_inactivity_kill_sec: float = 3600
    cc_agent_progress_throttle_sec: float = 10
    cc_max_turns: int = 100
    session_mapping_path: str = "./session_mapping.json"
    session_mapping_max_size: int = 5000  # each interaction records multiple response chunks
    shutdown_timeout_sec: int = 7  # Gives the service manager time to stop cleanly.
    topic_config_path: str = "./topic_config.json"
    notification_chat_id: int | None = None
    tmux_sessions_dir: str = "./tmux_sessions"
    # Choices offered by /model, in button order. Override with a JSON list in
    # `MODEL_PRESETS` to match whatever ids the local CLI build accepts — the
    # bot only validates syntax, never the id itself. A trailing `[1m]` is the
    # CLI's 1M-context flag and is preserved verbatim.
    model_presets: list[str] = [
        "anthropic/claude-opus-5[1m]",
        "anthropic/claude-fable-5[1m]",
        "openrouter/moonshotai/kimi-k3[1m]",
        "openrouter/openai/gpt-5.5[1m]",
        "openrouter/x-ai/grok-4.6[1m]",
        "selectel/z-ai/glm-5.3",
    ]
    # Auto-retry for transient Claude Code API errors. When CC writes an
    # `isApiErrorMessage` transcript entry matching a known-transient pattern
    # (e.g. "Invalid `signature` in `thinking` block"), the bot nudges the
    # session with `api_error_retry_prompt` instead of just surfacing the raw
    # error, up to `api_error_retry_limit` consecutive times with exponential
    # backoff (2s doubling, capped at 20s). 0 disables. The consecutive
    # counter resets on forward progress, on a new user turn, and when a new
    # error arrives more than `api_error_retry_reset_sec` after the previous
    # one (new incident — covers manual recovery typed directly into the
    # tmux pane, which the bot cannot observe).
    api_error_retry_limit: int = 10
    api_error_retry_prompt: str = "continue"
    api_error_retry_reset_sec: float = 300.0


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
