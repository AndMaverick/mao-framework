"""The reference scenario must be reproducible and its arithmetic must follow from its assumptions."""

import importlib.util
import os
import sys
import tempfile
import unittest
from decimal import Decimal

from mao import PriceBook, Usage

_PATH = os.path.join(os.path.dirname(__file__), "..", "examples", "dispute_desk.py")
_spec = importlib.util.spec_from_file_location("dispute_desk", _PATH)
assert _spec and _spec.loader
dd = importlib.util.module_from_spec(_spec)
sys.modules["dispute_desk"] = dd
_spec.loader.exec_module(dd)

PRICES = PriceBook(dd.ASSUMPTIONS["prices_per_million"])


class DisputeDesk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = dd.make_cases(7, 600)
        cls.gov = dd.run_governed(cls.cases, PRICES)
        cls.base = dd.run_ungoverned(cls.cases, "premium", PRICES)

    def test_same_seed_same_result(self):
        again = dd.run_governed(dd.make_cases(7, 600), PRICES)
        self.assertEqual(again["llm_usd"], self.gov["llm_usd"])
        self.assertEqual(again["audit"].head, self.gov["audit"].head)

    def test_different_seed_different_cases(self):
        self.assertNotEqual([c.amount for c in dd.make_cases(8, 50)], [c.amount for c in dd.make_cases(7, 50)])

    def test_governed_never_duplicates_a_credit(self):
        self.assertEqual(self.gov["duplicate_credits"], 0)

    def test_baseline_duplicates_exactly_the_timeouts_it_retried(self):
        expected = [c for c in self.cases if c.submit_timeout and c.draft_failures < 3]
        self.assertEqual(self.base["duplicate_credits"], len(expected))
        self.assertAlmostEqual(self.base["duplicate_usd"], sum(c.amount for c in expected), places=2)

    def test_each_unresolved_credit_is_one_credit_not_two(self):
        reconcile = [c for c in self.cases if c.submit_timeout and c.draft_failures < 3
                     and c.amount <= dd.ASSUMPTIONS["approval_limit_usd"]]
        self.assertEqual(self.gov["reconciliations"], len(reconcile))

    def test_large_credits_wait_for_a_human(self):
        large = [c for c in self.cases if c.amount > dd.ASSUMPTIONS["approval_limit_usd"] and c.draft_failures < 3]
        self.assertEqual(self.gov["manual_review_over_limit"], len(large))
        self.assertEqual(self.base["credits_over_limit_without_approval"], len(large))

    def test_audit_chain_verifies_and_detects_tampering(self):
        audit = self.gov["audit"]
        self.assertTrue(audit.verify())
        anchor = audit.anchor()
        self.assertTrue(audit.verify(head=anchor["head"], length=anchor["length"]))
        forged = type(audit).from_jsonl(audit.to_jsonl())
        forged.entries[5]["cost"] = 0.0
        self.assertFalse(forged.verify())

    def test_governed_spend_recomputes_from_tokens(self):
        total = Decimal(0)
        for e in self.gov["audit"].entries:
            if e["event"] == "usage" and not e["estimated"]:
                u = Usage(e["input_tokens"], e["output_tokens"], e["cached_input_tokens"], e["cache_write_tokens"])
                total += Decimal(repr(PRICES.cost(e["model"], u)))
        metered = sum(Decimal(repr(e["cost"])) for e in self.gov["audit"].entries
                      if e["event"] == "usage" and not e["estimated"])
        self.assertEqual(total, metered)
        self.assertAlmostEqual(float(metered), self.gov["llm_usd"], places=6)

    def test_failed_attempts_are_charged(self):
        failed = [e for e in self.gov["audit"].entries if e["event"] == "usage" and e["outcome"] == "error"
                  and e["step"] == "draft"]
        self.assertTrue(failed)
        self.assertTrue(all(e["cost"] > 0 for e in failed))

    def test_net_effect_terms_add_up_and_follow_assumptions(self):
        n = dd.net_effect(self.base, self.gov)
        self.assertAlmostEqual(n["net"], n["llm_saved"] + n["duplicate_loss_avoided"] - n["manual_cost_added"], places=6)
        loss = 1 - dd.ASSUMPTIONS["duplicate_recovery_rate"]
        self.assertAlmostEqual(n["duplicate_loss_avoided"], self.base["duplicate_usd"] * loss, places=6)

    def test_csv_export_feeds_token_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "usage.csv")
            dd.run_governed(dd.make_cases(7, 40), PRICES, path)
            with open(path, encoding="utf-8") as fh:
                header = fh.readline().strip().split(",")
            self.assertIn("workload", header)
            self.assertIn("cost", header)


if __name__ == "__main__":
    unittest.main()
