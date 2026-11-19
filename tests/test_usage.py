"""Metering, exact pricing, provider adapters and the export into Token Ledger."""

import csv
import os
import tempfile
import unittest
from types import SimpleNamespace as NS

from mao import (
    Action,
    AuditLog,
    MissingPrice,
    ModelRegistry,
    ModelSpec,
    Orchestrator,
    Policy,
    PriceBook,
    Step,
    Usage,
    usage_from_anthropic,
    usage_from_openai,
    usage_rows,
    write_usage_csv,
)

X = frozenset({"x"})
PRICES = {"m": {"input_per_million": 2.0, "output_per_million": 10.0,
                "cached_input_per_million": 0.2, "cache_write_per_million": 2.5}}


class Pricing(unittest.TestCase):
    def test_every_token_type_priced_by_hand(self):
        # 1M in 2.00 + 4M cached 0.80 + 1M write 2.50 + 1M out 10.00
        cost = PriceBook(PRICES).cost("m", Usage(1_000_000, 1_000_000, 4_000_000, 1_000_000))
        self.assertAlmostEqual(cost, 15.30, places=9)

    def test_decimal_arithmetic_has_no_float_drift(self):
        book = PriceBook({"p": {"input_per_million": 0.1, "output_per_million": 0.0}})
        self.assertEqual(book.cost("p", Usage(3_000_000, 0)), 0.3)  # naive floats give 0.30000000000000004

    def test_a_missing_price_is_an_error_never_the_standard_rate(self):
        book = PriceBook({"p": {"input_per_million": 1.0, "output_per_million": 1.0}})
        with self.assertRaises(MissingPrice):
            book.cost("p", Usage(10, 10, cached_input_tokens=5))
        with self.assertRaises(MissingPrice):
            book.cost("p", Usage(10, 10, cache_write_tokens=5))
        with self.assertRaises(MissingPrice):
            book.cost("ghost", Usage(1, 1))

    def test_bad_usage_and_prices_rejected(self):
        for bad in (-1, 1.5, True, "3"):
            with self.assertRaises(ValueError):
                Usage(input_tokens=bad)
        with self.assertRaises(ValueError):
            PriceBook({"p": {"input_per_million": -1, "output_per_million": 1}})
        with self.assertRaises(ValueError):
            PriceBook({"p": {"input_per_million": 1}})

    def test_a_token_ledger_price_file_loads(self):
        book = PriceBook({"_note": "x", "tier_multipliers": {"batch": 0.5}, **PRICES})
        self.assertIn("m", book)
        self.assertNotIn("tier_multipliers", book)


class Adapters(unittest.TestCase):
    def test_anthropic_input_excludes_cache(self):
        resp = NS(usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=400, cache_creation_input_tokens=50))
        self.assertEqual(usage_from_anthropic(resp), Usage(100, 20, 400, 50))

    def test_openai_input_includes_cache_so_it_is_subtracted(self):
        resp = {"usage": {"prompt_tokens": 500, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 400}}}
        self.assertEqual(usage_from_openai(resp), Usage(100, 20, 400, 0))

    def test_openai_responses_api_shape(self):
        resp = NS(usage=NS(input_tokens=500, output_tokens=7, input_tokens_details=NS(cached_tokens=100)))
        self.assertEqual(usage_from_openai(resp), Usage(400, 7, 100, 0))

    def test_inconsistent_or_missing_usage_is_refused(self):
        with self.assertRaises(ValueError):
            usage_from_openai({"usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 99}}})
        with self.assertRaises(ValueError):
            usage_from_anthropic(NS())


def run_metered(cost_flat=0.0, prices=PRICES, budget=None, fn=None, retries=1):
    audit = AuditLog(clock=lambda: 1_767_225_600.0)  # 2026-01-01 UTC
    reg = ModelRegistry([ModelSpec("m", X, cost_flat, 10, 2, provider="acme")])
    pol = Policy("r", frozenset({"r"}))

    def action(ctx):
        ctx["report_usage"](Usage(1_000_000, 100_000, 2_000_000, 0))
        return "done"
    step = Step("s", Action("s", "r", 1, False, fn or action), needs=X, retries=retries)
    orch = Orchestrator(reg, pol, audit, budget=budget, prices=PriceBook(prices) if prices else None,
                        workload="disputes", team="Ops")
    return orch.run([step]), audit


class Metering(unittest.TestCase):
    def test_reported_usage_is_charged_exactly(self):
        # 2.00 + 1.00 + 0.40 = 3.40
        result, audit = run_metered()
        self.assertEqual((result.ok, result.spend), (True, 3.4))
        usage = next(e for e in audit.entries if e["event"] == "usage")
        self.assertEqual((usage["estimated"], usage["input_tokens"], usage["cached_input_tokens"]), (False, 1_000_000, 2_000_000))
        self.assertTrue(audit.verify())

    def test_no_report_falls_back_to_the_flat_estimate_and_says_so(self):
        result, audit = run_metered(cost_flat=0.5, fn=lambda c: "x")
        self.assertEqual(result.spend, 0.5)
        self.assertTrue(next(e for e in audit.entries if e["event"] == "usage")["estimated"])

    def test_unpriceable_usage_halts_instead_of_guessing(self):
        result, audit = run_metered(prices={"other": {"input_per_million": 1.0, "output_per_million": 1.0}})
        self.assertFalse(result.ok)
        self.assertIn("cannot meter spend", result.reason)
        self.assertTrue(any(e["event"] == "usage.unpriced" for e in audit.entries))

    def test_a_failed_attempt_that_reported_tokens_is_still_charged(self):
        def fails(ctx):
            ctx["report_usage"](Usage(1_000_000, 0, 0, 0))
            raise RuntimeError("model call succeeded, parsing failed")
        result, _ = run_metered(fn=fails, retries=2)
        self.assertEqual((result.ok, round(result.spend, 9), result.attempts), (False, 4.0, 2))

    def test_budget_halts_after_the_attempt_that_crosses_it(self):
        result, _ = run_metered(budget=3.0)
        self.assertEqual((result.ok, result.reason), (False, "budget exceeded"))
        self.assertAlmostEqual(result.spend, 3.4)


class Export(unittest.TestCase):
    def test_rows_match_the_audit_and_load_in_a_csv_reader(self):
        _, audit = run_metered()
        rows = usage_rows(audit)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["date"], rows[0]["workload"], rows[0]["team"], rows[0]["provider"]),
                         ("2026-01-01", "disputes", "Ops", "acme"))
        path = os.path.join(tempfile.mkdtemp(), "u.csv")
        self.assertEqual(write_usage_csv(audit, path), 1)
        with open(path, encoding="utf-8", newline="") as fh:
            row = next(csv.DictReader(fh))
        self.assertEqual((float(row["cost"]), row["estimated"]), (3.4, "False"))

    def test_formula_injection_is_neutralised(self):
        audit = AuditLog()
        audit.append("usage", step="s", attempt=1, model="=cmd|' /C calc'!A0", provider="p", workload="+x", team="@y",
                     outcome="ok", estimated=False, cost=1.0, input_tokens=1, cached_input_tokens=0,
                     cache_write_tokens=0, output_tokens=1)
        path = os.path.join(tempfile.mkdtemp(), "u.csv")
        write_usage_csv(audit, path)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("'=cmd", text)
        self.assertIn("'+x", text)
        self.assertIn("'@y", text)


if __name__ == "__main__":
    unittest.main()
