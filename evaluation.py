"""Search-quality evaluation (stdlib only, no database): IR metrics + an in-memory retriever that mirrors
``index.search_local`` (AND query -> OR fallback -> rerank with ``scoring.score_candidates``).

It lets ranking/stemming/semantic changes be *measured* on a labelled query set instead of guessed. The in-memory
BM25 stands in for PostgreSQL ``ts_rank_cd``; it is a proxy, so use the numbers to compare configurations against each
other, not as absolute production quality.
"""
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .oromoo import MIN_PREFIX, looks_oromoo, stem
from .scoring import score_candidates
from ..core.textutil import content_tokens, host, tokens

# ------------------------------------------------------------------ metrics


def dcg(gains: Sequence[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int = 10) -> float:
    """Graded nDCG@k. 1.0 = the ideal ordering; a query with no relevant documents scores 0.0."""
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    return dcg([relevant.get(u, 0) for u in ranked[:k]]) / ideal if ideal else 0.0


def mrr(ranked: Sequence[str], relevant: Mapping[str, int]) -> float:
    """Reciprocal rank of the first relevant result (0.0 if none is returned)."""
    for i, u in enumerate(ranked, 1):
        if relevant.get(u, 0) > 0:
            return 1.0 / i
    return 0.0


def recall_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int = 10) -> float:
    rel = {u for u, g in relevant.items() if g > 0}
    return len(rel & set(ranked[:k])) / len(rel) if rel else 0.0


# ------------------------------------------------------------------ in-memory retriever


@dataclass
class Config:
    name: str
    stem_expansion: bool = False  # Oromoo queries: stem-prefix matching (the in-memory twin of fts_query_om)
    use_stems: bool = False  # stem-aware title match in the reranker
    semantic_weight: float = 0.0  # vector-similarity signal in the reranker
    k1: float = 1.2
    b: float = 0.75
    title_boost: int = 3  # a title token counts like this many body tokens
    extra: dict = field(default_factory=dict)


class MemoryIndex:
    def __init__(self, docs: Sequence[Mapping]):
        self.docs = list(docs)
        self._tf: list[Counter] = []
        self._len: list[int] = []
        self._vocab_df: Counter = Counter()
        self._built_boost = None

    def _build(self, title_boost: int) -> None:
        if self._built_boost == title_boost:
            return
        self._tf, self._len, self._vocab_df = [], [], Counter()
        for d in self.docs:
            tf = Counter(tokens(d["body"]))
            for t in tokens(d["title"]):
                tf[t] += title_boost
            self._tf.append(tf)
            self._len.append(sum(tf.values()))
            self._vocab_df.update(tf.keys())
        self._built_boost = title_boost

    @staticmethod
    def _matches(term: str, is_last: bool, expand: bool, word: str) -> bool:
        s = stem(term)
        if expand and len(s) >= MIN_PREFIX and s != term:
            return word.startswith(s)
        return word.startswith(term) if is_last else word == term

    def _term_stats(self, i: int, term: str, is_last: bool, expand: bool) -> int:
        return sum(c for w, c in self._tf[i].items() if self._matches(term, is_last, expand, w))

    def _candidates(self, q_terms: list[str], expand: bool, mode: str, cfg: Config) -> list[dict]:
        n = len(self.docs)
        avg_len = (sum(self._len) / n) or 1.0
        rows = []
        for i, d in enumerate(self.docs):
            tfs = [self._term_stats(i, t, j == len(q_terms) - 1, expand) for j, t in enumerate(q_terms)]
            hit = all(tfs) if mode == "and" else any(tfs)
            if not hit:
                continue
            score = 0.0
            for j, (t, tf) in enumerate(zip(q_terms, tfs)):
                if not tf:
                    continue
                df = sum(1 for k in range(n) if self._term_stats(k, t, j == len(q_terms) - 1, expand))
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                norm = tf + cfg.k1 * (1 - cfg.b + cfg.b * self._len[i] / avg_len)
                score += idf * tf * (cfg.k1 + 1) / norm
            rows.append(
                {
                    "url": d["url"],
                    "domain": host(d["url"]),
                    "title": d["title"],
                    "rank": score / (score + 1),  # squash like ts_rank_cd normalisation 32
                    "inlinks": 0,
                    "fetched_at": 0.0,
                }
            )
        return rows

    def search(self, q: str, cfg: Config, lang_hint: str | None = None, limit: int = 10) -> list[str]:
        self._build(cfg.title_boost)
        q_terms = content_tokens(q)[:12]
        if not q_terms:
            return []
        expand = cfg.stem_expansion and looks_oromoo(q, lang_hint)
        rows = self._candidates(q_terms, expand, "and", cfg)
        if len(rows) < 5:  # same widening rule as search_local
            seen = {r["url"] for r in rows}
            rows += [r for r in self._candidates(q_terms, expand, "or", cfg) if r["url"] not in seen]
        # fetched_at=0 for every doc -> freshness identical for all, so it cannot affect the comparison
        scored = score_candidates(rows, q, now=0.0, use_stems=cfg.use_stems, semantic_weight=cfg.semantic_weight)
        return [r["url"] for r in scored[:limit]]


# ------------------------------------------------------------------ running a dataset


def load_dataset(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def evaluate(dataset: Mapping, cfg: Config, k: int = 10, lang: str | None = None) -> dict:
    """Mean nDCG@k / MRR / Recall@k over the dataset's queries (optionally only those with ``lang``)."""
    idx = MemoryIndex(dataset["docs"])
    per_query = []
    for item in dataset["queries"]:
        if lang and item.get("lang") != lang:
            continue
        ranked = idx.search(item["q"], cfg, lang_hint=item.get("lang"), limit=k)
        rel = item["relevant"]
        per_query.append(
            {
                "q": item["q"],
                "lang": item.get("lang"),
                "ndcg": ndcg_at_k(ranked, rel, k),
                "mrr": mrr(ranked, rel),
                "recall": recall_at_k(ranked, rel, k),
                "top": ranked[:3],
            }
        )
    n = len(per_query) or 1
    return {
        "config": cfg.name,
        "queries": len(per_query),
        "ndcg": sum(p["ndcg"] for p in per_query) / n,
        "mrr": sum(p["mrr"] for p in per_query) / n,
        "recall": sum(p["recall"] for p in per_query) / n,
        "per_query": per_query,
    }


def compare(dataset: Mapping, configs: Sequence[Config], k: int = 10, lang: str | None = None) -> list[dict]:
    return [evaluate(dataset, c, k, lang) for c in configs]


DEFAULT_CONFIGS: tuple[Config, ...] = (
    Config("baseline (exact terms)"),
    Config("+ stem expansion", stem_expansion=True),
    Config("+ stem expansion + stem title match", stem_expansion=True, use_stems=True),
    Config("+ all + semantic 0.5", stem_expansion=True, use_stems=True, semantic_weight=0.5),
    Config("+ all + semantic 1.0", stem_expansion=True, use_stems=True, semantic_weight=1.0),
)


def format_table(results: Sequence[dict]) -> str:
    rows = [f"{'configuration':<42} {'nDCG@10':>8} {'MRR':>7} {'R@10':>7}  n"]
    for r in results:
        rows.append(f"{r['config']:<42} {r['ndcg']:>8.3f} {r['mrr']:>7.3f} {r['recall']:>7.3f}  {r['queries']}")
    return "\n".join(rows)
