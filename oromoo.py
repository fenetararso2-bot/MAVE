"""Afaan Oromoo text handling (stdlib only, no database): normalisation, tokenising, light stemming, query expansion.

This is a *light, rule-based* suffix stripper meant for conflating inflected forms (``barataa`` / ``barattoota``,
``mana`` / ``manatti`` / ``manicha``). It is NOT a full morphological analyser: roots that change shape
(``bulaa`` / ``bultoota``) and most verb morphology are not handled. Treat the suffix lists as a starting point for
review by a native speaker / linguist, and measure any change with ``eval_search.py``.
"""
import re
import unicodedata

from ..core.textutil import STOP_EN, STOP_OM, content_tokens, ts_term

# Typographic apostrophes / modifier letters / backticks all stand for the Oromoo hudhaa (glottal stop): ta'e, ba'a.
_APOS = re.compile("[\u2018\u2019\u201b\u02bb\u02bc\u02b9\u2032`\u00b4]")
# Letters, digits; an apostrophe is kept only *inside* a word (so quotes around a word are dropped).
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_VOWELS = "aeiou"

# Longest suffix wins. Possessives (koo, kee, isaa...) are written as separate words in Oromoo, so they are not here.
_PLURAL = ("oonni", "ootni", "ootaa", "oolii", "oota", "ooli", "onni", "otni", "ota", "wwan", "lee")
_CASE = ("irraa", "rraa", "dhaaf", "dhaan", "itti", "tti", "iin", "ii", "f", "n")
_DEFINITE = ("ittii", "icha", "ichi")
_SUFFIXES = tuple(sorted(set(_PLURAL + _CASE + _DEFINITE), key=len, reverse=True))

MIN_STEM = 3          # a stem is never shorter than this
MIN_STEM_SINGLE = 4   # ...and stripping a single letter (-f, -n) needs a longer remainder
MIN_PREFIX = 4        # stems shorter than this are too broad to use as a tsquery prefix ("man:*" -> "management")

# Function words that are distinctively Oromoo (the shared STOP_OM list minus anything that is also English).
DISTINCT_OM = frozenset(STOP_OM - STOP_EN - {"an", "ta", "si"})


def normalize(text: str) -> str:
    """NFC, lower-case, every apostrophe variant -> ASCII ``'``."""
    return _APOS.sub("'", unicodedata.normalize("NFC", text or "")).lower()


def tokenize(text: str) -> list[str]:
    """Words with the hudhaa kept inside them (``ta'e`` is one token). Ranking-side only: the PostgreSQL index
    splits at apostrophes, so :func:`app.textutil.tokens` stays the tokenizer for full-text queries."""
    return _WORD.findall(normalize(text))


def stem(word: str) -> str:
    """Conflation key for one word. Always a prefix of the (normalised) word, which is what lets it double as a
    ``tsquery`` prefix term."""
    w = normalize(word)
    if len(w) <= MIN_STEM or "'" in w or not w.isalpha():
        return w  # short words, words with a hudhaa (ta'e, ba'a: mostly function words) and numbers stay as they are
    was_plural = False
    for _ in range(3):  # stacked suffixes: barattootaaf = baratt + oota + f
        for suf in _SUFFIXES:
            floor = MIN_STEM_SINGLE if len(suf) == 1 else MIN_STEM
            if w.endswith(suf) and len(w) - len(suf) >= floor:
                w = w[: -len(suf)]
                was_plural = was_plural or suf in _PLURAL
                break
        else:
            break
    # Agent nouns (-aa) insert a -t- before the plural: bulaa -> bultoota, barsiisaa -> barsiistoota
    # (barataa -> barattoota is the same rule with a geminate). Drop that t so both forms share a stem.
    if was_plural and len(w) - 1 >= MIN_STEM and w[-1] == "t" and w[-2] not in _VOWELS:
        w = w[:-1]
    trimmed = w.rstrip(_VOWELS)
    if len(trimmed) >= MIN_STEM:
        w = trimmed
    if len(w) > MIN_STEM and w[-1] == w[-2] and w[-1] not in _VOWELS:  # baratt -> barat (geminate -> single)
        w = w[:-1]
    return w


def stems(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text)]


def normalize_query_lang(value: str | None) -> str | None:
    """Clean the optional ``lang`` hint sent by clients: only ``om`` / ``en`` are meaningful, anything else is ignored
    (a bad hint must never make a search fail)."""
    v = (value or "").strip().lower()
    return v if v in ("om", "en") else None


def looks_oromoo(q: str, lang: str | None = None) -> bool:
    """Is this *query* Oromoo? Queries are too short for :func:`app.textutil.detect_lang`, so a single distinctive
    function word (``fi``, ``kan``, ``akka``...) or an explicit ``lang="om"`` is enough."""
    if lang == "om":
        return True
    return any(t in DISTINCT_OM for t in tokenize(q))


def fts_query_om(q: str, mode: str = "and") -> str | None:
    """Like :func:`app.textutil.fts_query`, but a term whose stem is long enough becomes a *stem prefix* match, so
    ``barataa`` finds ``barattoota``. The stem is always a prefix of its word, so ``'stem':*`` also still matches the
    exact word. Every term is quoted (never an operator). The last term stays a prefix match, as before."""
    toks = content_tokens(q)[:12]
    if not toks:
        return None
    parts = []
    for i, t in enumerate(toks):
        s = stem(t)
        if len(s) >= MIN_PREFIX and s != t:
            parts.append(ts_term(s, prefix=True))
        else:
            parts.append(ts_term(t, prefix=i == len(toks) - 1))
    return (" | " if mode == "or" else " & ").join(parts)
