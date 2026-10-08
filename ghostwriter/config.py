from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_range(value: str) -> tuple[int, int]:
    """Parse "60-600" into (60, 600). A single number means a fixed value."""
    parts = [p.strip() for p in value.split("-")]
    if len(parts) == 1:
        lo = hi = int(parts[0])
    elif len(parts) == 2:
        lo, hi = int(parts[0]), int(parts[1])
    else:
        raise ValueError(f"invalid range: {value!r}")
    if lo < 0 or hi < lo:
        raise ValueError(f"invalid range: {value!r}")
    return lo, hi


def parse_hours(value: str) -> tuple[time, time]:
    """Parse "09:00-23:00" into two times. The window may cross midnight."""
    try:
        start, end = (time.fromisoformat(p.strip()) for p in value.split("-"))
    except ValueError as exc:
        raise ValueError(f"invalid ACTIVE_HOURS: {value!r}") from exc
    return start, end


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5"
    # Optional proxy/gateway (e.g. LiteLLM). Empty = Anthropic API directly.
    anthropic_base_url: str | None = None

    # Telegram userbot
    tg_api_id: int
    tg_api_hash: SecretStr
    tg_session_path: Path = Path("./secrets/ivan.session")

    # Control bot (optional when only the MCP connector is used)
    control_bot_token: SecretStr | None = None
    owner_tg_id: int = 0

    # Contact
    vladimir_tg_id: int = 0  # default contact; can be changed in the bot with /contact
    tg_chat: str | None = None
    owner_name: str = "Ivan"
    contact_name: str = "Vladimir"

    # Email
    vladimir_email: str | None = None
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: SecretStr | None = None
    imap_inbox_folder: str = "INBOX"
    imap_sent_folder: str | None = "Sent"
    email_poll_seconds: int = 60
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    email_from: str | None = None

    # Storage / files
    database_url: str = "sqlite+aiosqlite:///./data/ghostwriter.db"
    persona_dir: Path = Path("./persona")

    # Behaviour
    timezone: str = "Europe/Helsinki"
    active_hours: str = "09:00-23:00"
    reply_delay_seconds: str = "60-600"
    debounce_seconds: int = 30
    # Prompt size for drafts (the main token cost): see README "Расход токенов"
    history_limit: int = 15            # messages of context before the ones being answered
    style_sample_size: int = 15        # owner's recent hand-typed messages as style reference
    reply_batch_limit: int = 6         # at most this many newest unanswered messages are answered at once
    draft_message_chars: int = 700     # long messages / forwarded posts are cut to this in draft prompts
    escalation_history: int = 8        # context messages for the sensitive-topic check
    backfill_on_start: int = 200
    auto_mode: bool = False
    log_level: str = "INFO"

    # MCP server (connector for the Claude apps)
    mcp_enabled: bool = False
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8765
    mcp_token: SecretStr | None = None
    mcp_allow_send: bool = False

    @field_validator("control_bot_token", "anthropic_api_key", "mcp_token", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("anthropic_base_url", "tg_chat", "vladimir_email", "imap_host", "smtp_host", "email_from", "imap_sent_folder", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("owner_tg_id", "vladimir_tg_id", mode="before")
    @classmethod
    def _empty_to_zero(cls, v: object) -> object:
        return 0 if isinstance(v, str) and not v.strip() else v

    @property
    def bot_enabled(self) -> bool:
        return self.control_bot_token is not None and bool(self.control_bot_token.get_secret_value().strip())

    @property
    def llm_enabled(self) -> bool:
        return self.anthropic_api_key is not None and bool(self.anthropic_api_key.get_secret_value().strip())

    def missing_for_run(self) -> list[str]:
        """What must be set before `run`. Two modes: control bot (needs an API key) and/or MCP connector."""
        missing = []
        if not self.bot_enabled and not self.mcp_enabled:
            missing.append("CONTROL_BOT_TOKEN or MCP_ENABLED=true")
        if self.bot_enabled:
            if not self.owner_tg_id:
                missing.append("OWNER_TG_ID")
            if not self.llm_enabled and not self.mcp_enabled:
                missing.append("ANTHROPIC_API_KEY")
        if self.mcp_enabled and (self.mcp_token is None or len(self.mcp_token.get_secret_value()) < 24):
            missing.append("MCP_TOKEN (24+ characters, e.g. `openssl rand -hex 24`)")
        return missing

    @field_validator("active_hours")
    @classmethod
    def _check_hours(cls, v: str) -> str:
        parse_hours(v)
        return v

    @field_validator("reply_delay_seconds")
    @classmethod
    def _check_delay(cls, v: str) -> str:
        parse_range(v)
        return v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {v!r}") from exc
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def active_window(self) -> tuple[time, time]:
        return parse_hours(self.active_hours)

    @property
    def reply_delay_range(self) -> tuple[int, int]:
        return parse_range(self.reply_delay_seconds)

    @property
    def watched_chat(self) -> str | int:
        """Telegram chat to watch: TG_CHAT if set, else the private chat with the contact."""
        if self.tg_chat is None:
            return self.vladimir_tg_id
        chat = self.tg_chat.strip()
        return int(chat) if chat.lstrip("-").isdigit() else chat

    @property
    def email_enabled(self) -> bool:
        return bool(self.imap_host and self.smtp_host and self.vladimir_email)

    @property
    def email_sender(self) -> str | None:
        return self.email_from or self.smtp_user


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
