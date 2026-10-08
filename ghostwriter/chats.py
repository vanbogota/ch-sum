"""Chat references: which person gets auto-drafts and which chat commands work on."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ChatRef:
    chat_id: int                 # Telegram peer id (users > 0, groups/channels < 0)
    title: str
    kind: str = "user"           # user | group | channel
    user_id: int | None = None   # whose messages get auto-drafts; for a private chat == chat_id

    @property
    def is_private(self) -> bool:
        return self.kind == "user"

    @property
    def label(self) -> str:
        icon = {"user": "👤", "group": "👥", "channel": "📢"}.get(self.kind, "💬")
        return f"{icon} {self.title}"

    def dumps(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def loads(cls, raw: str) -> ChatRef:
        return cls(**json.loads(raw))
