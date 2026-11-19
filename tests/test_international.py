"""Data residency, Arabic and Indonesian text, Gulf amounts, and governance controls. Expected values worked by hand.

Non-ASCII text is written as escapes so this file reads the same in any editor.
"""
import unittest
from datetime import date
from decimal import Decimal

from mao import Action, AuditLog, Orchestrator, Policy, Step, compliance, lingua, sovereign
from mao.model import ModelRegistry, ModelSpec
from mao.sovereign import Flow, Node, Residency

NODES = {n.name: n for n in [Node("app", "AE"), Node("vectordb", "AE"), Node("llm-api", "US"), Node("backup", "EU"), Node("logs", "SG")]}


class Residence(unittest.TestCase):
    rules = Residency({"personal": ["GCC", "EU"]}, groups={"GCC": ["AE", "SA", "QA", "BH", "KW", "OM"]}, derives={"prompt": ["personal"]})

    def test_a_flow_to_a_forbidden_jurisdiction_is_a_violation_at_that_end_only(self):
        v = sovereign.check(NODES, [Flow("app", "llm-api", "personal"), Flow("app", "vectordb", "personal")], self.rules)
        self.assertEqual([(x.node, x.jurisdiction) for x in v], [("llm-api", "US")])

    def test_both_ends_count(self):
        v = sovereign.check(NODES, [Flow("llm-api", "app", "personal")], self.rules)
        self.assertEqual([x.node for x in v], ["llm-api"])
        both = sovereign.check(NODES, [Flow("llm-api", "logs", "personal")], self.rules)
        self.assertEqual(sorted(x.node for x in both), ["llm-api", "logs"])

    def test_groups_expand(self):
        self.assertEqual(self.rules.allowed("personal"), frozenset({"AE", "SA", "QA", "BH", "KW", "OM", "EU"}))

    def test_a_prompt_inherits_the_rules_of_the_personal_data_in_it(self):
        v = sovereign.check(NODES, [Flow("app", "llm-api", "prompt")], self.rules)
        self.assertEqual(len(v), 1)
        self.assertIn("a prompt carries personal", v[0].reason)

    def test_unclassified_data_is_surfaced_not_silently_free(self):
        flows = [Flow("app", "logs", "telemetry"), Flow("app", "llm-api", "prompt")]
        self.assertEqual(sovereign.unclassified(flows, self.rules), ["telemetry"])
        self.assertEqual(sovereign.check(NODES, [Flow("app", "logs", "telemetry")], self.rules), [])

    def test_unknown_nodes_are_violations(self):
        v = sovereign.check(NODES, [Flow("app", "ghost", "personal")], self.rules)
        self.assertEqual(v[0].reason, "unknown node 'ghost'")

    def test_derivation_cycles_terminate(self):
        r = Residency({"a": ["AE"]}, derives={"a": ["b"], "b": ["a"]})
        self.assertEqual(r.classes_for("a"), ["a", "b"])

    def test_exposure_egress_and_reach(self):
        flows = [Flow("app", "vectordb", "personal"), Flow("app", "llm-api", "personal"), Flow("llm-api", "logs", "telemetry")]
        self.assertEqual(sovereign.exposure(NODES, flows), {"personal": ["AE", "US"], "telemetry": ["SG", "US"]})
        self.assertEqual([f.dst for f in sovereign.egress(NODES, flows, "AE")], ["llm-api"])
        self.assertEqual(sovereign.reach(flows, "app"), ["llm-api", "logs", "vectordb"])
        self.assertEqual(sovereign.reach(flows, "app", "personal"), ["llm-api", "vectordb"])

    def test_render_and_duplicates(self):
        text = sovereign.render(NODES, [Flow("app", "llm-api", "personal")], self.rules, home="AE")
        self.assertIn("Violations: 1", text)
        self.assertIn("Leaves AE", text)
        with self.assertRaises(ValueError):
            sovereign.pair([Node("a", "AE"), Node("a", "EU")])


A = "أحمد"  # the name Ahmad, with an alef with hamza above
A_BARE = "احمد"


class Arabic(unittest.TestCase):
    def test_variants_converge(self):
        self.assertEqual(lingua.normalize(A), A_BARE)
        self.assertEqual(lingua.normalize("مُحَمَّد"), lingua.normalize("محمّد"))  # diacritics
        self.assertEqual(lingua.normalize("مــحمد"), "محمد")  # tatweel
        self.assertEqual(lingua.normalize("١٢٣ ۴۵"), "123 45")
        self.assertEqual(lingua.normalize("١٬٥٠٠٫٥"), "1,500.5")

    def test_ta_marbuta_is_opt_in(self):
        word = "مدرسة"
        self.assertEqual(lingua.normalize(word), word)
        self.assertEqual(lingua.normalize(word, ta_marbuta=True), "مدرسه")
        self.assertEqual(lingua.search_key("  " + A + "  X "), A_BARE + " x")

    def test_ratio_and_scripts(self):
        self.assertAlmostEqual(lingua.arabic_ratio(A_BARE + "ab"), 4 / 6)  # four Arabic letters, two Latin
        profile = lingua.script_profile(A_BARE + "ab")
        self.assertEqual(profile, {"ARABIC": 0.6667, "LATIN": 0.3333})
        self.assertEqual(lingua.dominant_script(A_BARE + "ab"), "ARABIC")
        self.assertIsNone(lingua.dominant_script(A_BARE + "abc" + "中文", 0.6))
        self.assertEqual(lingua.script_profile("123 !!"), {})
        self.assertEqual(lingua.script_profile("中文"), {"CJK": 1.0})


class Amounts(unittest.TestCase):
    def test_gulf_amounts(self):
        self.assertEqual(lingua.parse_amount("Fee: AED 1,500 per month"), (1500, "AED"))
        self.assertEqual(lingua.parse_amount("about 1.5 million dirhams"), (1500000, "AED"))
        self.assertEqual(lingua.parse_amount("500 SAR"), (500, "SAR"))
        self.assertEqual(lingua.parse_amount("٢٠٠ ريال"), (200, "SAR"))
        self.assertEqual(lingua.parse_amount("١٫٥ مليون درهم"), (1500000, "AED"))
        self.assertEqual(lingua.parse_amount("2.25 million USD"), (2250000, "USD"))

    def test_no_currency_is_an_error_not_a_guess(self):
        with self.assertRaises(ValueError):
            lingua.parse_amount("1,500 units")


class Indonesian(unittest.TestCase):
    def test_rupiah(self):
        cases = {"Rp 1.500.000": 1500000, "sekitar 1,5 juta": 1500000, "gajinya 12 jt per bulan": 12000000, "500 rb": 500000, "2 miliar": 2 * 10**9,
                 "Rp. 25.000": 25000}
        for text, value in cases.items():
            self.assertEqual(lingua.parse_rupiah(text), value, text)
        self.assertEqual(lingua.parse_rupiah("1,25 juta"), 1250000)
        self.assertEqual(lingua.parse_rupiah("0,5 ribu"), Decimal("500"))
        with self.assertRaises(ValueError):
            lingua.parse_rupiah("tidak ada angka")
        with self.assertRaises(ValueError):
            lingua.parse_rupiah("12 orang")

    def test_format_round_trip(self):
        self.assertEqual(lingua.format_rupiah(1500000), "Rp 1.500.000")
        self.assertEqual(lingua.format_rupiah(-2500), "-Rp 2.500")
        self.assertEqual(lingua.parse_rupiah(lingua.format_rupiah(987654321)), 987654321)

    def test_slang_is_conservative(self):
        self.assertEqual(lingua.normalize_slang("Gak udah deh, Budi bgt"), "tidak sudah deh, Budi sangat")
        self.assertEqual(lingua.normalize_slang("Gagah"), "Gagah")

    def test_dates(self):
        self.assertEqual(lingua.parse_date("Lahir 17 Agustus 2026"), date(2026, 8, 17))
        self.assertEqual(lingua.parse_date("tgl 3 Des 2025"), date(2025, 12, 3))
        self.assertEqual(lingua.format_date(date(2026, 8, 17)), "17 Agustus 2026")
        with self.assertRaises(ValueError):
            lingua.parse_date("31 Februari 2026")  # no such day
        with self.assertRaises(ValueError):
            lingua.parse_date("hari ini")

    def test_phones_converge(self):
        for raw in ("0812-3456-7890", "+62 812 3456 7890", "62812 3456 7890", "+62 (0) 812-3456-7890"):
            self.assertEqual(lingua.normalize_phone(raw), "+6281234567890", raw)
        for raw in ("+1 415 555 0100", "0812", "+62 1"):
            with self.assertRaises(ValueError):
                lingua.normalize_phone(raw)


FULL = {"model": "triage-v2", "risk_class": "high", "risk_management": True, "data_governance": True, "bias_score": 0.04, "audit_logging": True,
        "transparency_notice": True, "human_oversight": True, "data_minimization": True, "owner": "Platform AI"}


class Controls(unittest.TestCase):
    def test_complete_evidence_is_ready_under_every_framework(self):
        for fw in compliance.FRAMEWORKS:
            self.assertEqual(compliance.readiness(compliance.scan(FULL, fw)), 1.0)

    def test_missing_and_failed_are_distinguished(self):
        statuses = {f.control: f.status for f in compliance.scan({**FULL, "audit_logging": False, "bias_score": 0.3}, "eu-ai-act")}
        self.assertEqual((statuses["record-keeping"], statuses["bias-testing"]), ("missing", "failed"))
        self.assertEqual(compliance.scan({**FULL, "bias_score": "n/a"}, "gdpr")[0].status, "met")  # gdpr has no bias control
        self.assertEqual([f.status for f in compliance.scan({**FULL, "bias_score": "n/a"}, "eu-ai-act") if f.control == "bias-testing"], ["failed"])
        self.assertEqual(compliance.readiness(compliance.scan({"x": 1}, "gdpr")), 0.0)

    def test_frameworks_differ_and_unknown_is_refused(self):
        self.assertIn("data-minimization", compliance.controls_for("gdpr"))
        self.assertNotIn("data-minimization", compliance.controls_for("eu-ai-act"))
        with self.assertRaises(ValueError):
            compliance.scan(FULL, "made-up")

    def test_bias_numbers_by_hand(self):
        outcomes = [1] * 8 + [0] * 2 + [1] * 4 + [0] * 6  # group a: 8 of 10 = 0.8 ; group b: 4 of 10 = 0.4
        groups = ["a"] * 10 + ["b"] * 10
        r = compliance.bias_report(outcomes, groups, min_group_size=30)
        self.assertEqual((r["rates"], r["bias_score"], r["disparate_impact_ratio"]), ({"a": 0.8, "b": 0.4}, 0.4, 0.5))
        self.assertTrue(r["below_four_fifths"])
        self.assertEqual(r["small_groups"], ["a", "b"])
        fair = compliance.bias_report([1, 0, 1, 0], ["a", "a", "b", "b"], 1)
        self.assertEqual((fair["bias_score"], fair["disparate_impact_ratio"], fair["below_four_fifths"]), (0.0, 1.0, False))
        self.assertIsNone(compliance.bias_report([0, 0], ["a", "b"], 1)["disparate_impact_ratio"])

    def test_bad_bias_input(self):
        for call in (lambda: compliance.bias_report([1], ["a", "b"]), lambda: compliance.bias_report([], []), lambda: compliance.bias_report([2], ["a"])):
            with self.assertRaises(ValueError):
                call()

    def test_report(self):
        text = compliance.render("m", "nist-ai-rmf", compliance.scan({"model": "m"}, "nist-ai-rmf"), {"bias_score": 0.2})
        self.assertIn("MISSING", text)
        self.assertIn("- [ ]", text)
        self.assertIn("bias_score: 0.2", text)
        self.assertIn("No open gaps.", compliance.render("m", "gdpr", compliance.scan(FULL, "gdpr")))

    def test_runtime_evidence_comes_from_what_actually_ran(self):
        policy = Policy("ops", frozenset({"r"}), max_risk=3, approval_risk=2)
        reg = ModelRegistry([ModelSpec("m", frozenset({"x"}), 0.0, 10, 3)])
        audit = AuditLog()
        self.assertEqual(compliance.runtime_evidence(audit, policy)["audit_logging"], False)  # empty log is not evidence
        act = Action("pay", "r", 2, True, lambda c: "ok")
        Orchestrator(reg, policy, audit=audit, approver=lambda s: True).run([Step("s", act, needs=frozenset({"x"}))])
        ev = compliance.runtime_evidence(audit, policy)
        self.assertEqual((ev["audit_logging"], ev["human_oversight"], ev["approvals_recorded"]), (True, True, 1))
        audit.entries[1]["step"] = "tampered"
        self.assertFalse(compliance.runtime_evidence(audit, policy)["audit_logging"])
        no_gate = Policy("ops", frozenset({"r"}), max_risk=1, approval_risk=2)
        self.assertFalse(compliance.runtime_evidence(AuditLog(), no_gate)["human_oversight"])


if __name__ == "__main__":
    unittest.main()
