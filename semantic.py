"""Vector similarity for hybrid search (stdlib only).

``Embedder`` is the seam for a real semantic model (a provider embedding API, a local sentence-transformer, pgvector /
OpenSearch k-NN later on). Until one is configured, ``HashingEmbedder`` is the default: a fastText-style bag of
*sub-word* character n-grams of the stemmed words, hashed into a fixed-size sparse vector.

Be clear about what that is: it captures spelling / morphological / partial-word similarity (``postgres`` ~
``postgresql``, ``barataa`` ~ ``barattoota``, small typos), NOT meaning (``car`` ~ ``automobile``). It needs no
model download, no network and no training data, and is deterministic, which makes it a safe default and a useful
baseline for ``eval_search.py``.
"""
import math
import zlib
from typing import Protocol

from .oromoo import stem, tokenize

Vector = dict[int, float]  # sparse: bucket -> weight, L2-normalised


class Embedder(Protocol):
    def embed(self, text: str) -> Vector: ...


class HashingEmbedder:
    def __init__(self, dim: int = 1024, ngrams: tuple[int, ...] = (3, 4), use_stems: bool = True):
        self.dim = dim
        self.ngrams = ngrams
        self.use_stems = use_stems

    def _features(self, text: str):
        for tok in tokenize(text):
            word = stem(tok) if self.use_stems else tok
            padded = f"<{word}>"  # boundary markers: prefixes/suffixes get their own n-grams
            for n in self.ngrams:
                for i in range(max(1, len(padded) - n + 1)):
                    yield padded[i : i + n]

    def embed(self, text: str) -> Vector:
        vec: Vector = {}
        for feat in self._features(text):
            h = zlib.crc32(feat.encode("utf-8"))  # stable across processes, unlike hash()
            bucket = h % self.dim
            sign = 1.0 if (h >> 31) & 1 else -1.0  # signed hashing: collisions cancel instead of piling up
            vec[bucket] = vec.get(bucket, 0.0) + sign
        norm = math.sqrt(sum(v * v for v in vec.values()))
        return {k: v / norm for k, v in vec.items()} if norm else {}


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity of two L2-normalised sparse vectors, clamped to 0..1 (negative = unrelated)."""
    if len(a) > len(b):
        a, b = b, a
    return max(0.0, sum(v * b.get(k, 0.0) for k, v in a.items()))


DEFAULT_EMBEDDER: Embedder = HashingEmbedder()
