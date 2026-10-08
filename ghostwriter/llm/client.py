"""Thin async wrapper around the Anthropic Messages API."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import anthropic

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


class LLM:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        # Through a gateway, label each request with its purpose (LiteLLM turns tags into Langfuse trace tags).
        self.tag_requests = bool(base_url)
        # An empty ANTHROPIC_BASE_URL in the environment would override the SDK default with "".
        if not os.environ.get("ANTHROPIC_BASE_URL", "x").strip():
            os.environ.pop("ANTHROPIC_BASE_URL")
        # With api_key/base_url=None the SDK resolves them from the environment / its defaults.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    async def _create(self, *, system: str, user: str, max_tokens: int, purpose: str, **extra: Any) -> str:
        if self.tag_requests:
            extra["extra_headers"] = {"x-litellm-tags": f"ghostwriter,{purpose}"}
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # The system prompt (persona + rules) is stable between calls: cache it.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                **extra,
            )
        except anthropic.RateLimitError as exc:
            raise LLMError("Claude rate limit hit, try again later") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("cannot reach Claude API") from exc

        log.debug("claude usage: %s", response.usage)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise LLMRefusal(f"Claude declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            log.warning("Claude response truncated at max_tokens=%s", max_tokens)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            raise LLMError("empty response from Claude")
        return text

    async def text(self, system: str, user: str, max_tokens: int = 4000, purpose: str = "text") -> str:
        return await self._create(system=system, user=user, max_tokens=max_tokens, purpose=purpose)

    async def json(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 2000, purpose: str = "json"
    ) -> dict[str, Any]:
        """Structured output constrained to a JSON schema."""
        raw = await self._create(
            system=system,
            user=user,
            max_tokens=max_tokens,
            purpose=purpose,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("Claude returned invalid JSON") from exc


class DisabledLLM(LLM):
    """Used when no ANTHROPIC_API_KEY is configured (MCP-connector-only mode)."""

    def __init__(self) -> None:  # noqa: D107 - no client is created
        self.model = "-"
        self.tag_requests = False

    async def _create(self, *, system: str, user: str, max_tokens: int, purpose: str, **extra: Any) -> str:
        raise LLMError(
            "ANTHROPIC_API_KEY не задан: бот работает без Claude. "
            "Саммари и черновики делай в приложении Claude через MCP-коннектор."
        )

