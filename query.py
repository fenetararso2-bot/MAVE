"""Query analysis (stdlib only, no database): whitespace/apostrophe normalisation, language guess and a *category
hint* ("this looks like a news / images / videos / tech query").

The hint is advisory. It never changes which results are returned; clients may use it to suggest a tab ("Show news
results?"). It is deliberately conservative: it only answers when exactly one category has the strongest cue, and
returns ``None`` when nothing matches or when two categories tie ("tech news").

The Afaan Oromoo cue words below are a short starting list and must be reviewed by a native speaker, like the
stemmer suffixes in :mod:`app.oromoo`. Extend the sets from real query logs, and measure with ``eval_search.py``.
"""
from dataclasses import dataclass

from .oromoo import looks_oromoo, normalize, normalize_query_lang, tokenize
from ..core.textutil import STOP_EN, STOP_OM, content_tokens

MAX_QUERY_CHARS = 300

# English function words that are not also Oromoo ones. Oromoo is written in the Latin alphabet, so "plain ASCII" is
# NOT evidence of English; an English function word is.
_DISTINCT_EN = frozenset(STOP_EN - STOP_OM)

# Intent cues. Single words only (matched on normalised tokens). Ambiguous words (python, java, rust, swift, go,
# apple, ...) are left out on purpose: one such word alone says nothing about intent.
_CUES: dict[str, frozenset[str]] = {
    "news": frozenset(
        {
            "news", "breaking", "headlines", "headline", "latest", "today",
            "oduu", "har'a", "haaraa",  # Oromoo: news, today, new/latest
        }
    ),
    "images": frozenset(
        {
            "image", "images", "picture", "pictures", "photo", "photos", "wallpaper", "wallpapers", "logo",
            "fakkii", "fakkiiwwan", "suuraa",  # Oromoo: picture(s); "Suuraa" is also the Images tab label
        }
    ),
    "videos": frozenset(
        {
            "video", "videos", "watch", "trailer", "livestream", "youtube",
            "viidiyoo",  # Oromoo (loanword): video
        }
    ),
    "tech": frozenset(
        {
            "api", "sdk", "docker", "kubernetes", "kotlin", "javascript", "typescript", "github", "stackoverflow",
            "programming", "compiler", "debug", "exception", "traceback", "algorithm", "database", "framework",
            "tutorial", "install", "bug",
        }
    ),
}
INTENTS = tuple(_CUES)


@dataclass(frozen=True)
class QueryInfo:
    text: str                  # whitespace-collapsed, original casing, capped at MAX_QUERY_CHARS
    normalized: str            # lower-cased, every apostrophe variant -> ASCII '
    terms: tuple[str, ...]     # content terms (stop words removed unless that would leave nothing)
    lang: str | None           # "om" | "en" | None when unknown
    intent: str | None         # one of INTENTS, or None


def clean(q: str) -> str:
    """Collapse whitespace and cap the length. Never raises, never returns ``None``."""
    return " ".join((q or "").split())[:MAX_QUERY_CHARS]


def detect_intent(q: str) -> str | None:
    """Category hint for a query, or ``None`` if there is no clear winner (no cue, or a tie)."""
    toks = set(tokenize(q))
    hits = {name: len(toks & cues) for name, cues in _CUES.items()}
    best = max(hits.values(), default=0)
    if best == 0:
        return None
    winners = [name for name, n in hits.items() if n == best]
    return winners[0] if len(winners) == 1 else None


def guess_lang(q: str, hint: str | None = None) -> str | None:
    """``"om"`` when the hint says so or the query contains a distinctively Oromoo function word; ``"en"`` when the
    hint says so or the query contains a distinctively English function word (and no Oromoo one); otherwise
    ``None``. Short queries are ambiguous (``barnoota Oromiyaa`` is Oromoo but has no function word, ``python
    tutorial`` has no English one), so no guess is better than a wrong label. An explicit hint always wins."""
    hint = normalize_query_lang(hint)
    if hint:
        return hint
    if looks_oromoo(q):
        return "om"
    if any(t in _DISTINCT_EN for t in tokenize(q)):
        return "en"
    return None


def analyze(q: str, lang_hint: str | None = None) -> QueryInfo:
    """One pass over a user query. ``lang_hint`` is the optional client ``lang`` parameter (bad values are ignored)."""
    text = clean(q)
    return QueryInfo(
        text=text,
        normalized=normalize(text),
        terms=tuple(content_tokens(text)),
        lang=guess_lang(text, lang_hint),
        intent=detect_intent(text),
    )
