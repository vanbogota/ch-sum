"""Loads the owner-written persona files and escalation rules from PERSONA_DIR."""
from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EscalationCategory:
    name: str
    description: str
    patterns: tuple[re.Pattern[str], ...] = ()
    force: bool = False  # a keyword hit escalates even if the classifier disagrees

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in self.patterns)


@dataclass(frozen=True)
class Persona:
    context: str
    examples: str
    categories: tuple[EscalationCategory, ...] = field(default_factory=tuple)
    directory: Path | None = None

    @classmethod
    def load(cls, directory: Path) -> Persona:
        return cls(
            context=_read(directory / "context.md", directory / "context.example.md"),
            examples=_read(directory / "examples.md", directory / "examples.example.md"),
            categories=load_categories(directory / "escalation.toml"),
            directory=directory,
        )

    def contact_notes(self, chat_id: int | str) -> str:
        """persona/contacts/<chat id>.md: who this person / group is to the owner. Read on every call."""
        if self.directory is None:
            return ""
        path = self.directory / "contacts" / f"{chat_id}.md"
        return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _read(path: Path, fallback: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if fallback.exists():
        log.warning("%s not found, using %s. Write your own for better drafts.", path, fallback.name)
        return fallback.read_text(encoding="utf-8").strip()
    log.warning("%s not found", path)
    return ""


def load_categories(path: Path) -> tuple[EscalationCategory, ...]:
    if not path.exists():
        log.warning("%s not found: escalation relies on the drafter only", path)
        return ()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cats = []
    for item in data.get("category", []):
        cats.append(
            EscalationCategory(
                name=item["name"],
                description=item.get("description", ""),
                patterns=tuple(re.compile(p, re.IGNORECASE) for p in item.get("keywords", [])),
                force=bool(item.get("force", False)),
            )
        )
    return tuple(cats)
