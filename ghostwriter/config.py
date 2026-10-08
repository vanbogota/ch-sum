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

    # Telegram userbot
    tg_api_id: int
    tg_api_hash: SecretStr
    tg_session_path: Path = Path("./secrets/ivan.session")

    # Control bot
    control_bot_token: SecretStr
    owner_tg_id: int = 0

    # Contact
    vladimir_tg_id: int = 0  # required for `run`; `login`/`chats` work without it
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
    history_limit: int = 40
    style_sample_size: int = 40
    backfill_on_start: int = 200
    auto_mode: bool = False
    log_level: str = "INFO"

    @field_validator("tg_chat", "vladimir_email", "imap_host", "smtp_host", "email_from", "imap_sent_folder", mode="before")
    @classmethod
    def _empty_to_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("owner_tg_id", "vladimir_tg_id", mode="before")
    @classmethod
    def _empty_to_zero(cls, v: object) -> object:
        return 0 if isinstance(v, str) and not v.strip() else v

    def missing_for_run(self) -> list[str]:
        missing = [name for name, value in (("OWNER_TG_ID", self.owner_tg_id), ("VLADIMIR_TG_ID", self.vladimir_tg_id)) if not value]
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
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
