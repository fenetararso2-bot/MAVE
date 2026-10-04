"""Query analysis tests (no database, no FastAPI): cleaning, language guess, category hint."""
import unittest

from app.search.query import INTENTS, MAX_QUERY_CHARS, QueryInfo, analyze, clean, detect_intent, guess_lang


class TestClean(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(clean("  hello \t  world\n"), "hello world")

    def test_caps_length(self):
        self.assertEqual(len(clean("a" * 1000)), MAX_QUERY_CHARS)

    def test_none_and_empty_never_raise(self):
        self.assertEqual(clean(None), "")
        self.assertEqual(clean(""), "")


class TestIntent(unittest.TestCase):
    def test_each_category_has_a_working_cue(self):
        self.assertEqual(detect_intent("latest football news"), "news")
        self.assertEqual(detect_intent("sunset wallpapers"), "images")
        self.assertEqual(detect_intent("avengers trailer"), "videos")
        self.assertEqual(detect_intent("docker compose tutorial"), "tech")

    def test_oromoo_cues(self):
        self.assertEqual(detect_intent("oduu har'a"), "news")
        self.assertEqual(detect_intent("fakkii Finfinnee"), "images")
        self.assertEqual(detect_intent("viidiyoo Oromoo"), "videos")

    def test_typographic_apostrophe_is_normalised(self):
        self.assertEqual(detect_intent("oduu har\u2019a"), "news")

    def test_no_cue_is_none(self):
        self.assertIsNone(detect_intent("capital of Ethiopia"))
        self.assertIsNone(detect_intent("barnoota Oromiyaa"))
        self.assertIsNone(detect_intent(""))

    def test_ambiguous_words_are_not_cues(self):
        # one ambiguous word alone must not claim an intent
        for q in ("python", "java", "apple", "rust"):
            self.assertIsNone(detect_intent(q), q)

    def test_tie_is_none(self):
        self.assertIsNone(detect_intent("docker news"))  # one tech cue vs one news cue

    def test_strongest_cue_wins(self):
        self.assertEqual(detect_intent("kotlin docker api latest"), "tech")  # 3 tech vs 1 news

    def test_cue_inside_a_longer_word_does_not_match(self):
        self.assertIsNone(detect_intent("newsletter"))
        self.assertIsNone(detect_intent("photography"))

    def test_intents_are_valid_search_types(self):
        # the hint is meant to be offered as a tab, so every value must be a type /search accepts
        accepted = {"web", "tech", "news", "images", "videos"}
        self.assertTrue(set(INTENTS) <= accepted)


class TestLang(unittest.TestCase):
    def test_hint_wins(self):
        self.assertEqual(guess_lang("hello", "om"), "om")
        self.assertEqual(guess_lang("akka", "en"), "en")

    def test_bad_hint_is_ignored(self):
        self.assertEqual(guess_lang("what is this", "xx"), "en")
        self.assertIsNone(guess_lang("python tutorial", "klingon"))

    def test_oromoo_function_word(self):
        self.assertEqual(guess_lang("barnoota fi hojii"), "om")

    def test_english_function_word(self):
        self.assertEqual(guess_lang("how to learn kotlin"), "en")

    def test_plain_ascii_without_cue_is_unknown_not_english(self):
        # Oromoo is written in Latin letters, so ASCII alone proves nothing
        self.assertIsNone(guess_lang("barnoota Oromiyaa"))
        self.assertIsNone(guess_lang("python tutorial"))

    def test_unknown_script_is_unknown(self):
        self.assertIsNone(guess_lang("\u12a2\u1275\u12ee\u1335\u12eb"))  # Ethiopic script, not handled yet

    def test_empty_is_unknown(self):
        self.assertIsNone(guess_lang(""))


class TestAnalyze(unittest.TestCase):
    def test_full_info(self):
        info = analyze("  Oduu   HAR\u2019A  fi  Oromiyaa ")
        self.assertIsInstance(info, QueryInfo)
        self.assertEqual(info.text, "Oduu HAR\u2019A fi Oromiyaa")
        self.assertEqual(info.normalized, "oduu har'a fi oromiyaa")
        self.assertEqual(info.lang, "om")
        self.assertEqual(info.intent, "news")
        self.assertNotIn("fi", info.terms)

    def test_terms_keep_everything_when_only_stop_words(self):
        self.assertEqual(analyze("what is the").terms, ("what", "is", "the"))

    def test_hint_flows_through(self):
        self.assertEqual(analyze("barnoota", "om").lang, "om")

    def test_garbage_never_raises(self):
        for q in ("", " ", "\x00\x01", "'''", "!!!", "\u202e\u202d", "a" * 5000, "\U0001f600"):
            info = analyze(q)
            self.assertLessEqual(len(info.text), MAX_QUERY_CHARS)

    def test_frozen(self):
        with self.assertRaises(Exception):
            analyze("x").lang = "om"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
