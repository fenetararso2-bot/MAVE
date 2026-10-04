"""Grounded AI answers: retrieved sources in, validated cited answer out. Provider-independent.

Flow: sanitise sources (safety.prepare_sources) -> delimited prompt -> providers.complete (xAI/Anthropic)
-> safety.postprocess (strip links/images, validate citations, detect "no evidence").
The numbering of ``sources`` in the result is exactly the numbering the model cited, so clients can link [n].
"""
import hashlib
import logging

from . import providers, safety
from ..core.cache import build_cache
from ..core.config import settings

log = logging.getLogger("mave.ai")
# 5 minutes, shared between users (and between workers when MAVE_REDIS_URL is set). Safe because answers depend only on (question, sources, language),
# never on who asks; nothing user-specific is ever put into a prompt.
_cache = build_cache("answer", ttl=300, maxsize=512)


def _cache_key(provider: str, model: str, query: str, lang: str, sources: list[dict]) -> str:
    material = "\x1f".join([provider, model, query, lang] + [f"{s['url']}\x1e{s['title']}\x1e{s['snippet']}" for s in sources])
    return hashlib.sha256(material.encode()).hexdigest()


async def grounded_answer(query: str, sources: list[dict], lang: str) -> dict:
    query = safety.clean_text(query, safety.MAX_QUERY)
    safe, withheld = safety.prepare_sources(sources)
    if withheld:
        log.warning("withheld the text of %d source(s) containing instruction-like content", withheld)
    if not safe:  # nothing to ground an answer on: do not spend a provider call
        return safety.insufficient_answer(lang, [])
    provider = settings.ai_provider
    key = None
    if provider:  # demo answers (no provider) are never cached
        model = settings.xai_model if provider == "xai" else settings.model
        key = _cache_key(provider, model, query, lang, safe)
        hit = await _cache.aget(key)
        if hit is not None:
            return hit
    system, user_text = safety.build_prompt(query, safe, lang)
    raw = await providers.complete(system, user_text)
    result = safety.postprocess(raw, safe, lang)
    if not result["grounded"] and not result["insufficient"]:
        log.info("answer carried no valid citation (ungrounded)")
    if key is not None:
        await _cache.aset(key, result)
    return result
