"""Duplicate-content detection: exact (SHA-256) and near-duplicate (64-bit SimHash, banded lookup in PostgreSQL)."""
import hashlib
import re
from collections import Counter

from ..db import Conn

MIN_NEAR_DUP_TOKENS = 100  # SimHash is unreliable on short pages: those only get exact-duplicate detection
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def token_count(text: str) -> int:
    return len(_WORD.findall((text or "").lower()))


def simhash(text: str, shingle: int = 3, max_tokens: int = 5000) -> int:
    """64-bit SimHash over word shingles (unsigned)."""
    words = _WORD.findall((text or "").lower())[:max_tokens]
    grams = [" ".join(words[i : i + shingle]) for i in range(max(1, len(words) - shingle + 1))]
    acc = [0] * 64
    for gram, weight in Counter(grams).items():
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            acc[bit] += weight if (h >> bit) & 1 else -weight
    return sum(1 << bit for bit in range(64) if acc[bit] > 0)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def to_signed(v: int) -> int:  # PostgreSQL BIGINT is a signed 64-bit value
    return v - (1 << 64) if v >= (1 << 63) else v


def to_unsigned(v: int) -> int:
    return v + (1 << 64) if v < 0 else v


def bands(v: int) -> list[tuple[int, int]]:
    """Four 16-bit bands. Two hashes within Hamming distance <= 3 share at least one band (pigeonhole)."""
    return [(i, (v >> (16 * i)) & 0xFFFF) for i in range(4)]


def store_fingerprint(con: Conn, url: str, sh: int | None) -> None:
    row = con.execute("SELECT id FROM documents WHERE url=?", (url,)).fetchone()
    if row is None:
        return
    con.execute("DELETE FROM simhash_bands WHERE doc_id=?", (row["id"],))
    if sh is not None:
        con.executemany(
            "INSERT INTO simhash_bands(band, value, doc_id) VALUES(?,?,?)", [(b, v, row["id"]) for b, v in bands(sh)]
        )


def find_near_duplicate(con: Conn, sh: int, exclude_url: str, distance: int = 3) -> str | None:
    """URL of an indexed page whose SimHash is within ``distance`` bits of ``sh`` (None if there is none)."""
    if distance <= 0:
        return None
    distance = min(distance, 3)  # the 4-band index only guarantees recall up to 3 differing bits
    clauses = " OR ".join("(b.band=? AND b.value=?)" for _ in range(4))
    args = [x for pair in bands(sh) for x in pair]
    rows = con.execute(
        f"""SELECT DISTINCT d.url, d.simhash FROM simhash_bands b JOIN documents d ON d.id=b.doc_id
            WHERE d.url<>? AND d.simhash IS NOT NULL AND ({clauses}) LIMIT 300""",
        [exclude_url, *args],
    ).fetchall()
    for r in rows:
        if hamming(sh, to_unsigned(r["simhash"])) <= distance:
            return r["url"]
    return None
