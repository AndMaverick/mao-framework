"""Live clients and the live dispute desk, tested against a local server that speaks each provider's wire format.

This proves the client code, the retry logic, the metering and the runner. It does not prove any provider's
current behaviour: only a real call does that.
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from mao.live import AnthropicClient, GeminiClient, OpenAICompatClient, ProviderError, client_for

KEY = "sk-test-SECRET-1234567890"
CATS = {"never made": "fraud", "twice": "duplicate_charge", "tracking": "not_received", "listing": "not_as_described", "cancel": "subscription_cancelled"}


def classify(prompt):
    body = prompt.lower()
    for needle, cat in CATS.items():
        if needle in body:
            return cat
    return "fraud"


class Mock(BaseHTTPRequestHandler):
    faults = []  # statuses to return before succeeding
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        req = json.loads(raw)
        Mock.seen.append((self.path, dict(self.headers), req))
        if Mock.faults:
            status = Mock.faults.pop(0)
            self.send_response(status)
            self.send_header("retry-after", "0")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": f"fault {status} for {KEY}"[:0] + "temporary"}}).encode())
            return
        if self.path.startswith("/v1/messages"):
            prompt = req["messages"][0]["content"]
            kind = "anthropic"
        elif self.path.endswith("/chat/completions"):
            prompt = req["messages"][-1]["content"]
            kind = "openai"
        else:
            prompt = req["contents"][0]["parts"][0]["text"]
            kind = "gemini"
        if prompt.startswith("Classify"):
            text = json.dumps({"category": classify(prompt.split("Dispute:")[-1]), "confidence": 0.9})
        else:
            text = json.dumps({"response": "The cardholder disputes this charge and supports the claim with the details provided in the statement."})
        n_in, n_out = 100 + len(prompt) // 4, 30
        if kind == "anthropic":
            body = {"id": "msg_1", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": n_in, "output_tokens": n_out}}
        elif kind == "openai":
            body = {"id": "c_1", "choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": n_in, "completion_tokens": n_out, "total_tokens": n_in + n_out}}
        else:
            body = {"candidates": [{"content": {"parts": [{"text": text}]}}], "usageMetadata": {"promptTokenCount": n_in, "candidatesTokenCount": n_out, "totalTokenCount": n_in + n_out}}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", f"req_{len(Mock.seen)}")
        self.end_headers()
        self.wfile.write(data)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Mock)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Mock.faults, Mock.seen = [], []


class Clients(ServerCase):
    def test_each_provider_shape_round_trips_with_usage(self):
        for client in (AnthropicClient(KEY, self.url), OpenAICompatClient("openai", KEY, self.url), OpenAICompatClient("deepseek", KEY, self.url),
                       GeminiClient(KEY, self.url)):
            reply = client.complete("m", "Classify this. Dispute: I never made this purchase", system="s")
            self.assertEqual(json.loads(reply.text)["category"], "fraud")
            self.assertGreater(reply.usage.input_tokens, 0)
            self.assertEqual(reply.usage.output_tokens, 30)
            self.assertTrue(reply.request_id)

    def test_the_key_goes_in_the_right_header_and_nowhere_else(self):
        AnthropicClient(KEY, self.url).complete("m", "hi")
        OpenAICompatClient("openai", KEY, self.url).complete("m", "hi")
        GeminiClient(KEY, self.url).complete("g", "hi")
        headers = [{k.lower(): v for k, v in h.items()} for _, h, _ in Mock.seen]
        self.assertEqual(headers[0].get("x-api-key"), KEY)
        self.assertEqual(headers[1].get("authorization"), f"Bearer {KEY}")
        self.assertEqual(headers[2].get("x-goog-api-key"), KEY)
        self.assertTrue(all(KEY not in json.dumps(body) for _, _, body in Mock.seen))
        self.assertNotIn(KEY, repr(AnthropicClient(KEY, self.url)))

    def test_openai_uses_max_completion_tokens_and_deepseek_max_tokens(self):
        OpenAICompatClient("openai", KEY, self.url).complete("m", "hi", max_tokens=77)
        OpenAICompatClient("deepseek", KEY, self.url).complete("m", "hi", max_tokens=77)
        self.assertEqual(Mock.seen[0][2]["max_completion_tokens"], 77)
        self.assertEqual(Mock.seen[1][2]["max_tokens"], 77)

    def test_rate_limits_and_server_errors_are_retried_then_succeed(self):
        Mock.faults = [429, 503]
        sleeps = []
        reply = AnthropicClient(KEY, self.url, sleep=sleeps.append).complete("m", "hi")
        self.assertEqual((reply.attempts, len(sleeps)), (3, 2))
        self.assertEqual(sleeps, [0.0, 0.0])  # Retry-After: 0 is honoured

    def test_a_client_error_is_not_retried_and_a_persistent_fault_gives_up(self):
        Mock.faults = [400]
        with self.assertRaises(ProviderError) as ctx:
            AnthropicClient(KEY, self.url, sleep=lambda s: None).complete("m", "hi")
        self.assertEqual((ctx.exception.status, ctx.exception.retriable, len(Mock.seen)), (400, False, 1))
        self.assertNotIn(KEY, str(ctx.exception))
        Mock.faults, Mock.seen = [500, 500, 500], []
        with self.assertRaises(ProviderError) as ctx:
            AnthropicClient(KEY, self.url, max_attempts=3, sleep=lambda s: None).complete("m", "hi")
        self.assertEqual((ctx.exception.status, len(Mock.seen)), (500, 3))

    def test_network_failures_are_retried(self):
        calls = []

        def flaky(url, headers, body, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionResetError("reset")
            return 200, {}, json.dumps({"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()

        reply = AnthropicClient(KEY, transport=flaky, sleep=lambda s: None).complete("m", "hi")
        self.assertEqual((reply.text, reply.attempts), ("ok", 3))

    def test_a_response_without_usage_is_refused_not_metered_as_zero(self):
        def no_usage(url, headers, body, timeout):
            return 200, {}, json.dumps({"content": [{"type": "text", "text": "ok"}]}).encode()

        with self.assertRaises(ProviderError) as ctx:
            AnthropicClient(KEY, transport=no_usage).complete("m", "hi")
        self.assertIn("cannot meter", str(ctx.exception))
        for body in (b"not json", b"[]"):
            with self.assertRaises(ProviderError):
                AnthropicClient(KEY, transport=lambda u, h, b, t, body=body: (200, {}, body)).complete("m", "hi")

    def test_construction_refuses_unsafe_setups(self):
        with self.assertRaises(ProviderError):
            AnthropicClient("")
        with self.assertRaises(ProviderError):
            AnthropicClient(KEY, "http://example.com")
        with self.assertRaises(ValueError):
            OpenAICompatClient("nope", KEY)
        with self.assertRaises(ValueError):
            AnthropicClient(KEY, self.url, max_attempts=0)
        self.assertIsInstance(client_for("kimi" if False else "moonshot", api_key=KEY), OpenAICompatClient)


def load_runner():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "live_dispute_desk.py")
    spec = importlib.util.spec_from_file_location("live_dispute_desk", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_dispute_desk"] = mod
    spec.loader.exec_module(mod)
    return mod


class Runner(ServerCase):
    def run_desk(self, out, extra=(), faults=()):
        Mock.faults = list(faults)
        prices = os.path.join(out, "prices.json")
        with open(prices, "w") as fh:
            json.dump({"mock-model": {"input_per_million": 3.0, "output_per_million": 15.0}}, fh)
        os.environ["ANTHROPIC_API_KEY"] = KEY
        code = load_runner().main(["--model", "mock-model", "--prices", prices, "--base-url", self.url, "--out", os.path.join(out, "run"), *extra])
        with open(os.path.join(out, "run", "results.json")) as fh:
            return code, json.load(fh)

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            code, data = self.run_desk(t)
            s, rows = data["summary"], {r["id"]: r for r in data["disputes"]}
            self.assertEqual(code, 0)
            self.assertTrue(s["audit_chain_verified"] and s["metering_matches"])
            self.assertEqual(rows["D11"]["status"], "blocked_before_model")  # the injection never reached a model
            self.assertTrue(rows["D06"]["human_queue"] and rows["D11"]["human_queue"])  # above the approval limit, and the blocked one
            self.assertEqual(rows["D06"]["status"], "halted")
            self.assertEqual(rows["D01"]["status"], "credited")
            self.assertEqual(s["model_calls"], 2 * 10 + 2)  # D11 makes no calls, D06 makes both before submit is denied
            scored = [r for r in rows.values() if "correct" in r]  # accuracy of the mock's keyword rule, so only the arithmetic is checked
            self.assertEqual(s["classification_accuracy"], sum(r["correct"] for r in scored) / len(scored))
            self.assertTrue(all(os.path.exists(os.path.join(t, "run", f)) for f in ("audit.jsonl", "usage.csv", "calls.csv", "summary.md")))
            self.assertTrue(all(KEY not in open(os.path.join(t, "run", f)).read() for f in os.listdir(os.path.join(t, "run"))))

    def test_personal_data_is_redacted_before_the_model_sees_it(self):
        with tempfile.TemporaryDirectory() as t:
            self.run_desk(t, ["--cases", "5"])
            prompts = " ".join(json.dumps(req) for _, _, req in Mock.seen)
            self.assertNotIn("4111 1111 1111 1111", prompts)
            self.assertNotIn("sam@example.com", prompts)

    def test_the_run_survives_a_rate_limit_and_still_meters_exactly(self):
        with tempfile.TemporaryDirectory() as t:
            code, data = self.run_desk(t, ["--cases", "3"], faults=[500])
            self.assertEqual(code, 0)
            self.assertTrue(data["summary"]["metering_matches"])

    def test_a_missing_price_stops_the_run_before_any_spend(self):
        with tempfile.TemporaryDirectory() as t:
            prices = os.path.join(t, "p.json")
            with open(prices, "w") as fh:
                json.dump({"other-model": {"input_per_million": 1, "output_per_million": 1}}, fh)
            os.environ["ANTHROPIC_API_KEY"] = KEY
            self.assertEqual(load_runner().main(["--model", "mock-model", "--prices", prices, "--base-url", self.url, "--out", t]), 2)
            self.assertEqual(Mock.seen, [])

    def test_the_budget_stops_new_disputes(self):
        with tempfile.TemporaryDirectory() as t:
            _, data = self.run_desk(t, ["--budget", "0.0001"])
            self.assertEqual(sum(r["status"] == "not_run" for r in data["disputes"]), 11)


if __name__ == "__main__":
    unittest.main()
