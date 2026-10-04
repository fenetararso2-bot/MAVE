"""Reranking of full-text candidates (stdlib only, no database import, so it is unit-testable and reusable by
``eval_search.py``). ``index.search_local`` fetches candidates from PostgreSQL and hands them to ``score_candidates``.
"""
import math
import os
import time
from typing import Mapping, Sequence

from .oromoo import stem, tokenize
from .semantic import DEFAULT_EMBEDDER, Embedder, cosine
from ..core.textutil import content_tokens

# ts_rank_cd is squashed into 0..1 (normalisation 32); this brings it to the scale of the other ranking signals.
RELEVANCE_SCALE = 4.0
TITLE_WEIGHT = 1.5
POPULARITY_WEIGHT = 0.25
FRESHNESS_WEIGHT = 0.3
FRESHNESS_HALF_LIFE_DAYS = 180


def semantic_weight_default() -> float:
    """Weight of the vector-similarity signal (query vs title). OFF (0) by default: the seed evaluation shows no
    measurable gain from the hashing embedder yet. Enable with ``MAVE_SEMANTIC_WEIGHT=0.5`` after validating on a
    real labelled dataset with ``python eval_search.py``."""
    try:
        return max(0.0, float(os.environ.get("MAVE_SEMANTIC_WEIGHT", "0")))
    except ValueError:
        return 0.0


def title_match(q_tokens: Sequence[str], title: str, use_stems: bool = True) -> float:
    """Fraction of query terms found in the title: as a substring (the original behaviour) or, with
    ``use_stems``, as a word with the same stem (``barataa`` matches ``barattoota``)."""
    if not q_tokens:
        return 0.0
    low = title.lower()
    title_stems = {stem(t) for t in tokenize(title)} if use_stems else set()
    hits = 0
    for t in q_tokens:
        if t in low or (use_stems and len(stem(t)) >= 3 and stem(t) in title_stems):
            hits += 1
    return hits / len(q_tokens)


def score_candidates(
    rows: Sequence[Mapping],
    q: str,
    now: float | None = None,
    *,
    use_stems: bool = True,
    semantic_weight: float | None = None,
    embedder: Embedder | None = None,
) -> list[dict]:
    """Rows need ``url, domain, title, rank, inlinks, fetched_at`` (``rank`` = full-text relevance, 0..1). Returns
    result dicts sorted best-first. ``semantic_weight=0`` switches the vector signal off."""
    now = time.time() if now is None else now
    sem_w = semantic_weight_default() if semantic_weight is None else semantic_weight
    emb = embedder or DEFAULT_EMBEDDER
    q_toks = content_tokens(q)
    q_vec = emb.embed(q) if sem_w else {}
    scored = []
    for r in rows:
        relevance = RELEVANCE_SCALE * r["rank"]
        title_hits = title_match(q_toks, r["title"], use_stems)
        popularity = POPULARITY_WEIGHT * math.log1p(r["inlinks"])
        age_days = max(0.0, (now - r["fetched_at"]) / 86400)
        freshness = FRESHNESS_WEIGHT * math.exp(-age_days / FRESHNESS_HALF_LIFE_DAYS)
        semantic = sem_w * cosine(q_vec, emb.embed(r["title"])) if sem_w else 0.0
        score = relevance + TITLE_WEIGHT * title_hits + popularity + freshness + semantic
        scored.append(
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": "",
                "thumbnail": None,
                "domain": r["domain"],
                "score": score,
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
