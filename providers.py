"""AI provider abstraction: one chat completion through the configured provider (xAI Grok or Anthropic).

Web search lives in ``app.search.web``. Prompt building, caching and answer validation live in ``app.ai.answer`` /
``app.ai.safety``; this layer only talks to the provider."""
import logging

import httpx

from . import xai
from ..core.config import settings
from ..core.errors import ProviderError

log = logging.getLogger("mave.ai")


async def complete(system: str, user_text: str) -> str:
    """One chat completion through the configured AI provider (xAI Grok or Anthropic).

    Prompt building, caching and answer validation live in app.ai.answer / app.ai.safety; this layer only talks to the
    provider. Raises ProviderError when no provider is configured in production, or when the provider fails.
    """
    provider = settings.ai_provider
    if provider is None:
        if settings.is_production:
            raise ProviderError("AI provider is not configured")
        return "Demo mode: set XAI_API_KEY (or ANTHROPIC_API_KEY) on the server to get real AI answers."
    return await (_xai_answer if provider == "xai" else _anthropic_answer)(system, user_text)


async def _xai_answer(system: str, user_text: str) -> str:
    # The key is only ever put in the Authorization header; no message below may contain it or upstream text,
    # because ProviderError messages are returned to API clients.
    payload = xai.build_request(settings.xai_model, system, user_text)
    try:
        async with httpx.AsyncClient(timeout=60) as client:  # reasoning models can take a while
            r = await client.post(
                xai.XAI_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.xai_key}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"AI provider unreachable: {type(e).__name__}") from e
    if r.status_code >= 400:
        try:
            hint = xai.error_hint(r.json(), settings.xai_key)
        except ValueError:
            hint = ""
        log.warning("xAI error %s (model %s): %s", r.status_code, settings.xai_model, hint or "no details")
        raise ProviderError(f"AI provider error {r.status_code}")
    try:
        return xai.extract_text(r.json())
    except ValueError as e:  # unreadable JSON or XAIResponseError
        log.warning("xAI reply unusable (model %s): %s", settings.xai_model, e)
        raise ProviderError("AI provider returned no usable answer") from e


async def _anthropic_answer(system: str, user_text: str) -> str:
    payload = {
        "model": settings.model,
        "max_tokens": 800,
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": settings.anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"AI provider unreachable: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise ProviderError(f"AI provider error {r.status_code}")
    return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
