"""xAI (Grok) integration: wire format, provider selection, error hygiene, and a guard against committed secrets.
The pure-format tests need nothing; the provider tests need httpx importable (they replace its client)."""
import asyncio
import importlib.util
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from app.ai import xai
from app.core.config import settings

# Taken from the shape documented at docs.x.ai (reasoning item + message item).
DOC_REPLY = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "output": [
        {"id": "", "type": "reasoning", "status": "completed", "summary": [{"type": "summary_text", "text": "THINKING"}]},
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Use a copy and a numeric sort [1].", "annotations": []}],
        },
    ],
}


class TestWireFormat(unittest.TestCase):
    def test_request_body(self):
        body = xai.build_request("grok-4.6", "SYS", "Q+sources", 500)
        self.assertEqual(
            body,
            {"model": "grok-4.6", "instructions": "SYS", "input": "Q+sources", "max_output_tokens": 500, "store": False},
        )
        self.assertNotIn("tools", body)  # answers come only from MAVE's sources, never xAI live search
        self.assertNotIn("search_parameters", body)
        self.assertEqual(xai.XAI_URL, "https://api.x.ai/v1/responses")

    def test_extracts_only_the_message_text_not_the_reasoning(self):
        self.assertEqual(xai.extract_text(DOC_REPLY), "Use a copy and a numeric sort [1].")

    def test_multiple_parts_and_messages_are_joined(self):
        data = {"output": [
            {"type": "message", "content": [{"type": "output_text", "text": "A "}, {"type": "output_text", "text": "B"}]},
            {"type": "message", "content": [{"type": "output_text", "text": " C"}]},
        ]}
        self.assertEqual(xai.extract_text(data), "A B C")

    def test_plain_string_content_and_incomplete_status_still_yield_text(self):
        self.assertEqual(xai.extract_text({"output": [{"type": "message", "content": "plain"}]}), "plain")
        partial = {"status": "incomplete", "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut o"}]}]}
        self.assertEqual(xai.extract_text(partial), "cut o")

    def test_unusable_replies_raise_xai_response_error(self):
        cases = {
            "refusal": {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
            "error object": {"error": {"message": "boom"}, "output": []},
            "empty": {"output": []},
            "only reasoning": {"output": [DOC_REPLY["output"][0]]},
            "blank text": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "  "}]}]},
            "no output key": {},
            "output not a list": {"output": "x"},
            "junk items": {"output": [None, 3, "s", {"type": "message", "content": [None, 5]}]},
            "not a dict": ["x"],
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(xai.XAIResponseError):
                xai.extract_text(data)

    def test_only_message_items_count_even_if_another_item_carries_output_text_parts(self):
        data = {"output": [
            {"type": "reasoning", "content": [{"type": "output_text", "text": "SECRET-THOUGHT"}]},
            {"type": "function_call", "content": [{"type": "output_text", "text": "TOOL-NOISE"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "visible"}]},
        ]}
        self.assertEqual(xai.extract_text(data), "visible")

    def test_failure_reasons_are_distinguishable_in_logs(self):
        refusal = {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
        with self.assertRaisesRegex(xai.XAIResponseError, "declined"):
            xai.extract_text(refusal)
        with self.assertRaisesRegex(xai.XAIResponseError, "reported an error"):
            xai.extract_text({"error": {"message": "boom"}, "output": []})
        with self.assertRaisesRegex(xai.XAIResponseError, "empty"):
            xai.extract_text({"output": []})
        # text wins over a refusal part in the same reply (partial answer + trailing refusal)
        mixed = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}, {"type": "refusal"}]}]}
        self.assertEqual(xai.extract_text(mixed), "ok")

    def test_xai_response_error_is_a_value_error(self):  # providers catches ValueError for JSON + domain errors
        self.assertTrue(issubclass(xai.XAIResponseError, ValueError))

    def test_error_hint_is_short_and_never_contains_the_secret(self):
        secret = "sekret-token-123"
        self.assertEqual(xai.error_hint({"error": {"message": "model not found"}}), "model not found")
        self.assertEqual(xai.error_hint({"error": "bad key"}), "bad key")
        self.assertNotIn(secret, xai.error_hint({"error": f"Incorrect key {secret} supplied"}, secret))
        self.assertLessEqual(len(xai.error_hint({"error": "x" * 5000})), 200)
        self.assertEqual(xai.error_hint(None), "")


class TestProviderSelection(unittest.TestCase):
    ENV = ("XAI_API_KEY", "ANTHROPIC_API_KEY", "MAVE_AI_PROVIDER", "MAVE_XAI_MODEL")

    def env(self, **kw):
        patcher = mock.patch.dict(os.environ, {k: v for k, v in kw.items()}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in self.ENV:
            if k not in kw:
                os.environ.pop(k, None)

    def test_auto_prefers_xai_then_anthropic_then_none(self):
        self.env(XAI_API_KEY="x", ANTHROPIC_API_KEY="a")
        self.assertEqual(settings.ai_provider, "xai")
        self.env(ANTHROPIC_API_KEY="a")
        self.assertEqual(settings.ai_provider, "anthropic")
        self.env()
        self.assertIsNone(settings.ai_provider)

    def test_forcing_a_provider_never_falls_back_silently(self):
        self.env(XAI_API_KEY="x", ANTHROPIC_API_KEY="a", MAVE_AI_PROVIDER="anthropic")
        self.assertEqual(settings.ai_provider, "anthropic")
        self.env(ANTHROPIC_API_KEY="a", MAVE_AI_PROVIDER="xai")  # xai forced but no key
        self.assertIsNone(settings.ai_provider)
        self.env(XAI_API_KEY="x", MAVE_AI_PROVIDER="nonsense")  # unknown value = auto
        self.assertEqual(settings.ai_provider, "xai")

    def test_model_default_and_override(self):
        self.env()
        self.assertEqual(settings.xai_model, "grok-4.6")
        self.env(MAVE_XAI_MODEL="grok-4.7")
        self.assertEqual(settings.xai_model, "grok-4.7")
        self.env(MAVE_XAI_MODEL="  ")
        self.assertEqual(settings.xai_model, "grok-4.6")


@unittest.skipUnless(importlib.util.find_spec("httpx"), "httpx not installed")
class TestXaiProvider(unittest.TestCase):
    KEY = "test-key-NOT-A-REAL-SECRET"

    def setUp(self):
        from app.ai import providers

        self.providers = providers

        class FakeResponse:
            def __init__(self, status=200, data=None, bad_json=False):
                self.status_code, self._data, self._bad = status, data, bad_json

            def json(self):
                if self._bad:
                    raise ValueError("not json")
                return self._data

        class FakeClient:
            calls: list = []
            reply = None

            def __init__(self, *a, **k):
                self.timeout = k.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                FakeClient.calls.append({"url": url, "json": json, "headers": headers, "timeout": self.timeout})
                if isinstance(FakeClient.reply, Exception):
                    raise FakeClient.reply
                return FakeClient.reply

        self.Resp, self.Client = FakeResponse, FakeClient
        FakeClient.calls = []
        for patcher in (
            mock.patch.object(providers.httpx, "AsyncClient", FakeClient),
            mock.patch.dict(os.environ, {"XAI_API_KEY": self.KEY, "MAVE_ENV": "development"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for k in ("ANTHROPIC_API_KEY", "MAVE_AI_PROVIDER", "MAVE_XAI_MODEL"):
            os.environ.pop(k, None)

    def run_answer(self, q):
        return asyncio.run(self.providers.complete(f"SYS-{q}", f"USER-{q}"))

    def test_request_goes_to_the_responses_endpoint_with_bearer_auth(self):
        self.Client.reply = self.Resp(200, DOC_REPLY)
        self.assertEqual(self.run_answer("a"), "Use a copy and a numeric sort [1].")
        (call,) = self.Client.calls
        self.assertEqual(call["url"], "https://api.x.ai/v1/responses")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {self.KEY}")
        self.assertEqual(call["timeout"], 60)
        body = call["json"]
        self.assertEqual((body["model"], body["store"], body["max_output_tokens"]), ("grok-4.6", False, 800))
        self.assertEqual((body["instructions"], body["input"]), ("SYS-a", "USER-a"))  # prompt passes through unchanged
        self.assertNotIn(self.KEY, repr(body))  # the key never travels in the body or prompt
        self.assertNotIn("tools", body)

    def test_http_errors_become_generic_provider_errors_and_log_a_key_free_hint(self):
        self.Client.reply = self.Resp(401, {"error": f"Incorrect API key {self.KEY}"})
        with self.assertLogs("mave.ai", level="WARNING") as logs, self.assertRaises(self.providers.ProviderError) as cm:
            self.run_answer("401")
        self.assertEqual(str(cm.exception), "AI provider error 401")  # this text is returned to API clients
        joined = "\n".join(logs.output)
        self.assertIn("401", joined)
        self.assertNotIn(self.KEY, joined)

    def test_bad_model_hint_reaches_the_log_not_the_client(self):
        self.Client.reply = self.Resp(404, {"error": {"message": "The model grok-9 does not exist"}})
        with self.assertLogs("mave.ai", level="WARNING") as logs, self.assertRaises(self.providers.ProviderError) as cm:
            self.run_answer("404")
        self.assertNotIn("grok-9", str(cm.exception))
        self.assertIn("does not exist", "\n".join(logs.output))

    def test_network_failure_unreadable_json_and_refusal(self):
        import httpx

        self.Client.reply = httpx.HTTPError("boom")
        with self.assertRaises(self.providers.ProviderError) as cm:
            self.run_answer("net")
        self.assertEqual(str(cm.exception), "AI provider unreachable: HTTPError")
        for reply in (
            self.Resp(200, bad_json=True),
            self.Resp(200, {"output": []}),
            self.Resp(200, {"output": [{"type": "message", "content": [{"type": "refusal", "refusal": "x"}]}]}),
        ):
            self.Client.reply = reply
            with self.subTest(reply=reply._data), self.assertLogs("mave.ai", level="WARNING"), \
                    self.assertRaises(self.providers.ProviderError) as cm:
                self.run_answer("bad")
            self.assertEqual(str(cm.exception), "AI provider returned no usable answer")

    def test_no_provider_gives_demo_text_in_dev_and_an_error_in_production(self):
        os.environ.pop("XAI_API_KEY")
        self.assertIn("XAI_API_KEY", self.run_answer("demo"))
        self.assertEqual(self.Client.calls, [])
        with mock.patch.dict(os.environ, {"MAVE_ENV": "production"}), self.assertRaises(self.providers.ProviderError):
            self.run_answer("demo-prod")


class TestNoSecretsInTheRepository(unittest.TestCase):
    """The key lives in backend/.env (git-ignored) only: never in source, docs, tests or the Android app."""

    ROOT = Path(__file__).resolve().parents[2]
    PATTERNS = [re.compile(r"xai-[A-Za-z0-9]{20,}"), re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")]
    SUFFIXES = {".py", ".kt", ".kts", ".md", ".yml", ".yaml", ".txt", ".example", ".toml", ".properties", ".xml", ".json", ".sh"}

    def test_no_api_keys_in_tracked_text_files(self):
        hits = []
        for path in self.ROOT.rglob("*"):
            if not path.is_file() or path.name == ".env" or any(p in (".git", "__pycache__", "build", ".gradle") for p in path.parts):
                continue
            if path.suffix not in self.SUFFIXES and path.name != ".env.example":
                continue
            text = path.read_text(errors="ignore")
            hits += [str(path.relative_to(self.ROOT)) for pat in self.PATTERNS if pat.search(text)]
        self.assertEqual(hits, [], "API key found in a file that would be committed")

    def test_env_files_are_git_ignored(self):
        lines = (self.ROOT / ".gitignore").read_text().split()
        self.assertIn(".env", lines)


if __name__ == "__main__":
    unittest.main()
