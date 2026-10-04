"""Search-core tests that need no database: Afaan Oromoo stemming / query expansion, the hashing embedder,
reranking and the evaluation harness. (PostgreSQL-backed retrieval tests live in test_engine.py.)"""
import json
import unittest
from pathlib import Path

from app.search.evaluation import Config, MemoryIndex, evaluate, load_dataset, mrr, ndcg_at_k, recall_at_k
from app.search.oromoo import DISTINCT_OM, fts_query_om, looks_oromoo, normalize, normalize_query_lang, stem, tokenize
from app.search.scoring import score_candidates, title_match
from app.search.semantic import HashingEmbedder, cosine

SEED = Path(__file__).resolve().parent.parent / "eval" / "seed_dataset.json"


class TestOromooText(unittest.TestCase):
    def test_normalize_apostrophes(self):
        self.assertEqual(normalize("Ta\u2019e BA`A \u02bcx"), "ta'e ba'a 'x")

    def test_tokenize_keeps_hudhaa_inside_words_only(self):
        self.assertEqual(tokenize("Ta\u2019e, \u201cOromoo\u201d foo_bar 2026"), ["ta'e", "oromoo", "foo", "bar", "2026"])

    def test_inflected_forms_share_a_stem(self):
        groups = [
            ("barataa", "barattoota", "barattoonni", "barattootaaf"),  # student(s)
            ("bulaa", "bultoota", "bultoonni"),  # agent noun: -aa -> -toota
            ("barsiisaa", "barsiistoota", "barsiistoonni"),
            ("mana", "manoota", "manicha", "manatti", "manaaf"),  # house
            ("kitaaba", "kitaabota"),
            ("Oromoo", "Oromoon", "Oromoof"),
            ("Oromiyaa", "Oromiyaatti", "Oromiyaarraa"),
            ("yunivarsiitii", "yunivarsiitiin"),
        ]
        for g in groups:
            with self.subTest(group=g):
                self.assertEqual(len({stem(w) for w in g}), 1, [stem(w) for w in g])

    def test_unrelated_words_keep_different_stems(self):
        self.assertNotEqual(stem("barataa"), stem("barsiisaa"))
        self.assertNotEqual(stem("mana"), stem("nama"))

    def test_stem_leaves_short_words_numbers_and_hudhaa_words_alone(self):
        for w in ("fi", "kan", "ta'e", "ba'a", "2026"):
            self.assertEqual(stem(w), w)

    def test_stem_is_always_a_prefix_of_the_word(self):
        # this is what lets 'stem':* act as a tsquery prefix term
        for w in ("barattootaaf", "manicha", "Oromiyaatti", "hojjattoota", "kitaabota", "afaanii", "kotlin", "coffee"):
            self.assertTrue(normalize(w).startswith(stem(w)), (w, stem(w)))

    def test_possessives_are_not_stripped(self):  # koo/kee/isaa are separate words, and "-isaa" ends real roots
        self.assertEqual(stem("barsiisaa"), "barsiis")
        self.assertEqual(stem("isaa"), "isaa")

    def test_looks_oromoo(self):
        self.assertTrue(looks_oromoo("barumsa fi nyaata"))
        self.assertTrue(looks_oromoo("kotlin", "om"))  # explicit hint wins
        self.assertFalse(looks_oromoo("what is an apple"))
        self.assertFalse(looks_oromoo("kotlin coroutines"))
        self.assertTrue(DISTINCT_OM.isdisjoint({"the", "and", "an"}))

    def test_fts_query_om_uses_stem_prefixes(self):
        self.assertEqual(fts_query_om("barataa yunivarsiitii"), "'barat':* & 'yunivarsiit':*")
        self.assertEqual(fts_query_om("barataa yunivarsiitii", "or"), "'barat':* | 'yunivarsiit':*")

    def test_fts_query_om_short_stems_stay_exact_and_last_term_stays_prefix(self):
        # "man:*" would match "management": stems under 4 letters are not expanded
        self.assertEqual(fts_query_om("mana barumsaa"), "'mana' & 'barums':*")
        self.assertEqual(fts_query_om("kotlin"), "'kotl':*")
        self.assertEqual(fts_query_om("fi"), "'fi':*")

    def test_fts_query_om_is_injection_safe(self):
        q = fts_query_om("barataa' & !(x) | 'y\\")
        self.assertIsNotNone(q)
        for term in q.split(" & "):  # every term is a quoted literal, never an operator
            self.assertTrue(term.startswith("'") and (term.endswith("'") or term.endswith("':*")), term)
        self.assertIsNone(fts_query_om("   "))


class TestQueryLangHint(unittest.TestCase):
    """The ``lang`` hint clients send to ``/search`` and ``/ai/answer`` (UI language) is sanitised before use."""

    def test_only_om_and_en_are_accepted(self):
        self.assertEqual(normalize_query_lang("om"), "om")
        self.assertEqual(normalize_query_lang("EN"), "en")
        self.assertEqual(normalize_query_lang(" om "), "om")

    def test_anything_else_is_ignored_not_an_error(self):
        for bad in (None, "", "am", "om-ET", "x" * 8, "';--"):
            self.assertIsNone(normalize_query_lang(bad), bad)

    def test_hint_makes_a_short_query_oromoo_but_english_hint_does_not_force_it(self):
        self.assertTrue(looks_oromoo("barataa", normalize_query_lang("om")))
        self.assertFalse(looks_oromoo("barataa", normalize_query_lang("en")))
        self.assertFalse(looks_oromoo("barataa", normalize_query_lang("zz")))


class TestHashingEmbedder(unittest.TestCase):
    def setUp(self):
        self.e = HashingEmbedder()

    def test_deterministic_and_normalised(self):
        v = self.e.embed("Afaan Oromoo")
        self.assertEqual(v, self.e.embed("Afaan Oromoo"))
        self.assertAlmostEqual(sum(x * x for x in v.values()), 1.0, places=6)
        self.assertEqual(self.e.embed(""), {})

    def test_subword_similarity(self):
        near = cosine(self.e.embed("postgres"), self.e.embed("PostgreSQL"))
        far = cosine(self.e.embed("postgres"), self.e.embed("injera teff"))
        self.assertGreater(near, far + 0.2)
        self.assertGreater(cosine(self.e.embed("barataa"), self.e.embed("barattoota")), 0.9)  # same stem

    def test_it_is_not_semantic(self):  # documents the limit: no synonym knowledge
        self.assertLess(cosine(self.e.embed("car"), self.e.embed("automobile")), 0.2)

    def test_cosine_clamped(self):
        self.assertEqual(cosine({}, {1: 1.0}), 0.0)
        self.assertLessEqual(cosine({1: 1.0}, {1: 1.0}), 1.0)


def _row(url, title, rank=0.2, inlinks=0, fetched_at=0.0):
    return {"url": url, "domain": "x.com", "title": title, "rank": rank, "inlinks": inlinks, "fetched_at": fetched_at}


class TestScoring(unittest.TestCase):
    def test_title_match_substring_and_stem(self):
        self.assertEqual(title_match(["kotlin", "coroutines"], "Kotlin coroutines guide", use_stems=False), 1.0)
        self.assertEqual(title_match(["barataa"], "Barattoota Oromiyaa", use_stems=False), 0.0)
        self.assertEqual(title_match(["barataa"], "Barattoota Oromiyaa", use_stems=True), 1.0)
        self.assertEqual(title_match([], "anything"), 0.0)

    def test_default_scoring_matches_the_original_formula(self):
        # relevance*4 + 1.5*title_hits + 0.25*log1p(inlinks) + 0.3*exp(-age/180); no stems, no semantic signal
        import math

        out = score_candidates([_row("https://x.com/a", "Search engines", rank=0.5, inlinks=3, fetched_at=0.0)],
                               "search engines", now=0.0, use_stems=False, semantic_weight=0)
        expected = 4.0 * 0.5 + 1.5 * 1.0 + 0.25 * math.log1p(3) + 0.3
        self.assertAlmostEqual(out[0]["score"], expected, places=9)

    def test_sorted_best_first_and_result_shape(self):
        rows = [_row("https://x.com/1", "Gardening"), _row("https://x.com/2", "Kotlin coroutines guide")]
        out = score_candidates(rows, "kotlin coroutines", now=0.0)
        self.assertEqual([r["url"] for r in out], ["https://x.com/2", "https://x.com/1"])
        self.assertEqual(set(out[0]), {"title", "url", "snippet", "thumbnail", "domain", "score"})

    def test_popularity_breaks_ties(self):
        rows = [_row("https://x.com/a", "Same"), _row("https://x.com/b", "Same", inlinks=2)]
        self.assertEqual(score_candidates(rows, "same", now=0.0)[0]["url"], "https://x.com/b")

    def test_semantic_signal_is_opt_in(self):
        rows = [_row("https://x.com/1", "Cooking pasta"), _row("https://x.com/2", "PostgreSQL tuning")]
        off = {r["url"]: r["score"] for r in score_candidates(rows, "postgres", now=0.0, semantic_weight=0)}
        on = {r["url"]: r["score"] for r in score_candidates(rows, "postgres", now=0.0, semantic_weight=1.0)}
        self.assertGreater(on["https://x.com/2"], off["https://x.com/2"])
        self.assertEqual(on["https://x.com/1"], off["https://x.com/1"])  # unrelated title gets nothing

    def test_semantic_weight_env_var(self):
        import os
        from unittest import mock

        from app.search.scoring import semantic_weight_default

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAVE_SEMANTIC_WEIGHT", None)
            self.assertEqual(semantic_weight_default(), 0.0)
        with mock.patch.dict(os.environ, {"MAVE_SEMANTIC_WEIGHT": "0.5"}):
            self.assertEqual(semantic_weight_default(), 0.5)
        with mock.patch.dict(os.environ, {"MAVE_SEMANTIC_WEIGHT": "garbage"}):
            self.assertEqual(semantic_weight_default(), 0.0)
        with mock.patch.dict(os.environ, {"MAVE_SEMANTIC_WEIGHT": "-3"}):
            self.assertEqual(semantic_weight_default(), 0.0)


class TestMetrics(unittest.TestCase):
    def test_ndcg(self):
        rel = {"a": 3, "b": 1}
        self.assertAlmostEqual(ndcg_at_k(["a", "b", "c"], rel), 1.0)
        self.assertLess(ndcg_at_k(["b", "a"], rel), 1.0)
        self.assertEqual(ndcg_at_k(["x", "y"], rel), 0.0)
        self.assertEqual(ndcg_at_k(["a"], {}), 0.0)

    def test_mrr_and_recall(self):
        rel = {"a": 2, "b": 1}
        self.assertEqual(mrr(["x", "a"], rel), 0.5)
        self.assertEqual(mrr(["x", "y"], rel), 0.0)
        self.assertEqual(recall_at_k(["a", "x", "b"], rel, k=2), 0.5)
        self.assertEqual(recall_at_k(["a", "b"], rel, k=5), 1.0)


class TestEvaluationHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_dataset(SEED)

    def test_seed_dataset_is_well_formed(self):
        urls = {d["url"] for d in self.data["docs"]}
        self.assertEqual(len(urls), len(self.data["docs"]))
        for item in self.data["queries"]:
            self.assertTrue(item["relevant"], item["q"])
            self.assertTrue(set(item["relevant"]) <= urls, item["q"])
            self.assertTrue(all(g in (1, 2, 3) for g in item["relevant"].values()))
        self.assertTrue(self.data["_note"].startswith("SEED"))  # never mistaken for a real benchmark

    def test_stem_expansion_finds_inflected_forms_the_baseline_misses(self):
        # "barataa" (student) vs the page's "barattoota/barattoonni": exact terms rank it below the other university
        # page; stem expansion puts it first.
        idx = MemoryIndex(self.data["docs"])
        target = "https://eval.example/om/barattoota-yunivarsiitii"
        stemmed = idx.search("barataa yunivarsiitii", Config("s", stem_expansion=True, use_stems=True), lang_hint="om")
        self.assertEqual(stemmed[0], target)
        q = next(i for i in self.data["queries"] if i["q"] == "barataa yunivarsiitii")
        gain = []
        for cfg in (Config("b"), Config("s", stem_expansion=True, use_stems=True)):
            ranked = idx.search(q["q"], cfg, lang_hint="om")
            gain.append(ndcg_at_k(ranked, q["relevant"]))
        self.assertGreater(gain[1], gain[0])

    def test_stemming_does_not_hurt_on_the_seed_set(self):
        base = evaluate(self.data, Config("b"), lang="om")
        full = evaluate(self.data, Config("s", stem_expansion=True, use_stems=True), lang="om")
        self.assertGreaterEqual(full["ndcg"], base["ndcg"])
        self.assertGreaterEqual(full["recall"], base["recall"])
        self.assertGreater(full["ndcg"], 0.9)

    def test_english_queries_are_unaffected_by_oromoo_expansion(self):
        base = evaluate(self.data, Config("b"), lang="en")
        full = evaluate(self.data, Config("s", stem_expansion=True, use_stems=True), lang="en")
        self.assertEqual([p["top"] for p in base["per_query"]], [p["top"] for p in full["per_query"]])

    def test_evaluate_reports_every_query(self):
        r = evaluate(self.data, Config("b"))
        self.assertEqual(r["queries"], len(self.data["queries"]))
        json.dumps(r["per_query"])  # results are JSON-serialisable (for reports)


if __name__ == "__main__":
    unittest.main()
