"""Playbooks and plans, assurance (evals, drift, incidents), context packing and the prompt vault.

Expected values are worked by hand.
"""

import unittest
from datetime import datetime, timedelta

from mao import (
    Action,
    AuditLog,
    Case,
    Chunk,
    ModelRegistry,
    ModelSpec,
    Orchestrator,
    Policy,
    PromptVault,
    Register,
    SpecError,
    Suite,
    categorical_psi,
    compile_playbook,
    drift_report,
    eval_gate,
    ks_stat,
    lint_playbook,
    load_plan,
    pack,
    psi,
    run_suite,
)
from mao.assure import contains, exact, numeric_close, regex, risk_score, severity
from mao.playbook import playbook_progress, playbook_to_spec
from mao.vault import RefError

X = frozenset({"x"})


def act(name, scope="r", risk=1, side=False, fn=None):
    return Action(name, scope, risk, side, fn or (lambda c: name))


SPEC = {"steps": [
    {"id": "read", "latency": 2, "cost": 0.5},
    {"id": "a", "depends_on": ["read"], "latency": 5, "cost": 1.0},
    {"id": "b", "depends_on": ["read"], "latency": 1, "cost": 0.25},
    {"id": "pay", "depends_on": ["a", "b"], "latency": 3, "cost": 0.0, "approval": True},
]}


class Plans(unittest.TestCase):
    def test_order_levels_cost_and_critical_path(self):
        plan = load_plan(SPEC)
        self.assertEqual(plan.order()[0], "read")
        self.assertEqual(plan.order()[-1], "pay")
        self.assertEqual(plan.levels(), [["read"], ["a", "b"], ["pay"]])
        self.assertEqual(plan.total_cost(), 1.75)
        self.assertEqual(plan.critical_path(), (["read", "a", "pay"], 10.0))  # 2 + 5 + 3, the slower branch decides
        self.assertEqual(plan.approvals(), ["pay"])

    def test_invalid_specs_are_refused(self):
        bad = [
            {},
            {"steps": [{"id": "a"}, {"id": "a"}]},
            {"steps": [{"id": "a", "depends_on": ["ghost"]}]},
            {"steps": [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]},
            {"steps": [{"id": "a", "cost": -1}]},
            {"steps": [{"id": "a", "cost": float("nan")}]},
            {"steps": [{"id": ""}]},
        ]
        for spec in bad:
            with self.assertRaises(SpecError, msg=str(spec)):
                load_plan(spec)

    def test_a_plan_runs_through_the_orchestrator(self):
        plan = load_plan({"steps": [{"id": "one", "needs": ["x"]}, {"id": "two", "depends_on": ["one"], "needs": ["x"]}]})
        actions = {"one": act("one", fn=lambda c: 1), "two": act("two", fn=lambda c: c["deps"]["one"] + 1)}
        reg = ModelRegistry([ModelSpec("m", X, 0.0, 10, 1)])
        result = Orchestrator(reg, Policy("p", frozenset({"r"}))).run(plan.to_steps(actions))
        self.assertTrue(result.ok)

    def test_a_step_without_an_action_is_an_error_not_skipped(self):
        with self.assertRaises(SpecError):
            load_plan(SPEC).to_steps({"read": act("read")})

    def test_audit_finds_gates_the_policy_would_not_enforce_and_gates_the_plan_forgot(self):
        policy = Policy("p", frozenset({"r"}), max_risk=2, approval_risk=2)
        plan = load_plan({"steps": [{"id": "ok", "approval": True}, {"id": "forgot"}, {"id": "phantom", "approval": True}, {"id": "denied"}]})
        actions = {
            "ok": act("ok", risk=2, side=True),
            "forgot": act("forgot", risk=2, side=True),
            "phantom": act("phantom", risk=1, side=False),
            "denied": act("denied", scope="admin"),
        }
        findings = plan.audit(actions, policy)
        self.assertEqual(len(findings), 3)
        self.assertTrue(any("forgot" in f and "does not declare" in f for f in findings))
        self.assertTrue(any("phantom" in f and "will not enforce" in f for f in findings))
        self.assertTrue(any("denied" in f and "denies" in f for f in findings))
        self.assertFalse(any(f.startswith("ok:") for f in findings))


PLAYBOOK = """---
owner: Finance Ops
version: 3
---
# Month end close

## Step 1: Collect
- [x] Export usage
- [ ] Export invoices
note about the step

## Step 2: Reconcile
- [ ] Compare totals
"""


class Playbooks(unittest.TestCase):
    def test_compile_progress_and_spec(self):
        pb = compile_playbook(PLAYBOOK)
        self.assertEqual(pb["title"], "Month end close")
        self.assertEqual(pb["meta"], {"owner": "Finance Ops", "version": "3"})
        self.assertEqual([s["title"] for s in pb["steps"]], ["Collect", "Reconcile"])
        self.assertEqual(pb["steps"][0]["notes"], ["note about the step"])
        self.assertEqual(playbook_progress(pb), (1, 3))
        self.assertEqual(lint_playbook(pb), [])
        plan = load_plan(playbook_to_spec(pb))
        self.assertEqual(plan.order(), ["step-1", "step-2"])

    def test_lint_finds_the_problems(self):
        issues = lint_playbook(compile_playbook("## Step 2: Skipped\n- [ ] x\n## Step 3: Empty\n"))
        self.assertIn("missing title (a single '# Title' line)", issues)
        self.assertIn("missing front matter: owner", issues)
        self.assertIn("step numbering: expected 1, found 2", issues)
        self.assertIn("step 3 has no checklist items", issues)
        self.assertEqual(lint_playbook(compile_playbook("# t\n")).count("no steps found"), 1)


class Evaluation(unittest.TestCase):
    def test_scorers(self):
        c = Case("c", "in", "Hello")
        self.assertEqual(exact(c, " Hello "), 1.0)
        self.assertEqual(exact(c, "hello"), 0.0)
        self.assertEqual(contains(c, "say HELLO there"), 1.0)
        self.assertEqual(regex(r"^H\w+$")(c, "Hello"), 1.0)
        self.assertEqual(numeric_close(0.01)(Case("n", 0, 1.0), "1.005"), 1.0)
        self.assertEqual(numeric_close(0.01)(Case("n", 0, 1.0), "abc"), 0.0)

    def test_a_crash_scores_zero_and_is_recorded(self):
        suite = Suite("s", [Case("ok", "a", "a"), Case("boom", "b", "b")])

        def system(x):
            if x == "b":
                raise RuntimeError("down")
            return x

        report = run_suite(suite, system)
        self.assertEqual(report.mean("exact"), 0.5)
        self.assertEqual(report.pass_rate("exact"), 0.5)
        self.assertTrue(report.errors["boom"].startswith("RuntimeError"))

    def test_out_of_range_scores_and_duplicate_ids_are_refused(self):
        with self.assertRaises(ValueError):
            run_suite(Suite("s", [Case("a", 1, 1)], {"bad": lambda c, o: 2.0}), lambda x: x)
        with self.assertRaises(ValueError):
            Suite("s", [Case("a", 1), Case("a", 2)])

    def test_gate_fails_on_a_mean_drop_and_names_cases_that_flipped(self):
        suite = Suite("s", [Case(str(i), i, i) for i in range(10)])
        base = run_suite(suite, lambda x: x)
        worse = run_suite(suite, lambda x: x if x >= 3 else -1)  # cases 0, 1, 2 now fail: mean 0.7
        verdict = eval_gate(base, worse)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.regressions[0][0:2], ("exact", 0.3))
        self.assertEqual(verdict.newly_failing, ["0", "1", "2"])

    def test_gate_reports_a_flipped_case_even_when_the_mean_holds(self):
        suite = Suite("s", [Case("a", 1, 1), Case("b", 2, 3), Case("c", 3, 3)])
        base = run_suite(suite, lambda x: x)  # a passes, b fails, c passes: 2/3
        cand = run_suite(suite, lambda x: 3 if x != 1 else 0)  # a fails, b passes, c passes: 2/3
        verdict = eval_gate(base, cand)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.newly_failing, ["a"])

    def test_a_missing_scorer_is_a_regression(self):
        a = run_suite(Suite("s", [Case("a", 1, 1)], {"exact": exact, "contains": contains}), lambda x: x)
        b = run_suite(Suite("s", [Case("a", 1, 1)], {"exact": exact}), lambda x: x)
        self.assertEqual(eval_gate(a, b).regressions, [("contains", "missing", None)])


class Drift(unittest.TestCase):
    def test_identical_samples_do_not_drift(self):
        sample = list(range(100))
        self.assertAlmostEqual(psi(sample, sample), 0.0, places=9)
        self.assertEqual(drift_report(sample, sample)["status"], "stable")

    def test_a_shifted_population_is_major(self):
        report = drift_report(list(range(100)), [v + 60 for v in range(100)])
        self.assertEqual(report["status"], "major")
        self.assertGreater(report["psi"], 0.25)

    def test_ks_by_hand(self):
        self.assertAlmostEqual(ks_stat([1, 2, 3], [2, 3, 4]), 1 / 3)
        self.assertEqual(ks_stat([1, 2], [10, 11]), 1.0)

    def test_categorical_psi_by_hand(self):
        # shares 0.5/0.5 against 0.9/0.1: (0.9-0.5)ln(0.9/0.5) + (0.1-0.5)ln(0.1/0.5) = 0.4*0.5878 + 0.4*1.6094
        self.assertAlmostEqual(categorical_psi({"a": 50, "b": 50}, {"a": 90, "b": 10}), 0.4 * 0.587787 + 0.4 * 1.609438, places=5)
        self.assertEqual(categorical_psi({"a": 1}, {"a": 5}), 0.0)

    def test_bad_inputs_are_refused(self):
        for call in (lambda: psi([], [1]), lambda: psi([1], []), lambda: psi([1, 2], [1], bins=1),
                     lambda: ks_stat([], [1]), lambda: categorical_psi({}, {"a": 1})):
            with self.assertRaises(ValueError):
                call()


class Incidents(unittest.TestCase):
    T0 = datetime(2026, 11, 1, 9, 0)

    def test_risk_and_severity_by_hand(self):
        self.assertEqual(risk_score(5, 5, 1), 125)  # 5 * 5 * (6 - 1)
        self.assertEqual(risk_score(2, 2, 5), 4)
        self.assertEqual([severity(s) for s in (60, 59, 30, 29, 12, 11)], ["SEV1", "SEV2", "SEV2", "SEV3", "SEV3", "SEV4"])
        with self.assertRaises(ValueError):
            risk_score(0, 3, 3)

    def test_sla_breach_mttr_and_postmortem(self):
        reg = Register()
        reg.open("i1", "Wrong credit issued", 5, 5, 1, self.T0)  # SEV1, 4 hour SLA
        reg.open("i2", "Slow responses", 2, 3, 3, self.T0)  # 2*3*3 = 18, SEV3, 72 hour SLA
        self.assertEqual([i.id for i in reg.breached(self.T0 + timedelta(hours=5))], ["i1"])
        self.assertEqual(reg.breached(self.T0 + timedelta(hours=1)), [])
        reg.resolve("i1", self.T0 + timedelta(hours=6), cause="stale rule", fix="rule removed")
        self.assertEqual(reg.breached(self.T0 + timedelta(hours=100))[0].id, "i2")
        self.assertEqual(reg.mttr_hours(), 6.0)
        pm = reg.postmortem("i1")
        self.assertIn("SEV1", pm)
        self.assertIn("stale rule", pm)
        self.assertIn("_to be completed_", reg.postmortem("i2"))

    def test_duplicates_and_time_travel_are_refused(self):
        reg = Register()
        reg.open("i", "t", 3, 3, 3, self.T0)
        with self.assertRaises(ValueError):
            reg.open("i", "t", 3, 3, 3, self.T0)
        with self.assertRaises(ValueError):
            reg.resolve("i", self.T0 - timedelta(hours=1))
        self.assertIsNone(Register().mttr_hours())


def chunk(i, text, score):
    return Chunk(i, text, score)


class Context(unittest.TestCase):
    def test_budget_is_never_exceeded_and_best_scores_win(self):
        chunks = [chunk("a", "red green blue", 0.9), chunk("b", "red green blue", 0.85), chunk("c", "yellow orange purple", 0.5)]
        packed = pack(chunks, budget=8)  # each chunk is 4 tokens
        self.assertEqual([c.id for c in packed.chunks], ["a", "b"])
        self.assertEqual((packed.used, [c.id for c in packed.dropped]), (8, ["c"]))

    def test_diversity_prefers_the_distinct_chunk_over_the_duplicate(self):
        chunks = [chunk("a", "red green blue", 0.9), chunk("b", "red green blue", 0.85), chunk("c", "yellow orange purple", 0.5)]
        self.assertEqual([c.id for c in pack(chunks, budget=8, diversity=1.0).chunks], ["a", "c"])

    def test_pinned_chunks_come_first_and_must_fit(self):
        chunks = [chunk("a", "one two three", 0.1), chunk("b", "four five six", 0.9)]
        self.assertEqual([c.id for c in pack(chunks, budget=4, pinned=["a"]).chunks], ["a"])
        with self.assertRaises(ValueError):
            pack(chunks, budget=3, pinned=["a"])
        with self.assertRaises(ValueError):
            pack(chunks, budget=10, pinned=["ghost"])

    def test_a_real_tokenizer_can_be_supplied(self):
        packed = pack([chunk("a", "x", 1.0), chunk("b", "y", 0.5)], budget=10, count=lambda t: 6)
        self.assertEqual([c.id for c in packed.chunks], ["a"])
        self.assertEqual(packed.text(), "x")


class Vault(unittest.TestCase):
    def test_commit_is_idempotent_and_rollback_never_rewrites(self):
        v = PromptVault()
        one = v.commit("p", "You are a careful analyst.")
        self.assertEqual(v.commit("p", "You are a careful analyst.").number, 1)
        two = v.commit("p", "You are a terse analyst.")
        back = v.rollback("p", 1)
        self.assertEqual((one.number, two.number, back.number), (1, 2, 3))
        self.assertEqual(back.text, one.text)
        self.assertEqual(v.get("p").number, 3)
        self.assertIn("rollback to v1", back.note)

    def test_refs_and_diff(self):
        v = PromptVault()
        a = v.commit("p", "line one\nline two")
        v.commit("p", "line one\nline 2")
        self.assertEqual(v.get("p", a.digest[:6]).number, 1)
        self.assertIn("-line two", v.diff("p", 1, 2))
        self.assertIn("+line 2", v.diff("p", 1, 2))
        for ref in (9, "nope"):
            with self.assertRaises(RefError):
                v.get("p", ref)
        with self.assertRaises(RefError):
            v.get("ghost")

    def test_deploy_is_gated_and_a_failing_gate_leaves_the_tag_alone(self):
        v = PromptVault()
        v.commit("p", "good")
        self.assertTrue(v.deploy("p", "prod", lambda ver: True))
        v.commit("p", "bad")
        self.assertFalse(v.deploy("p", "prod", lambda ver: False))
        self.assertFalse(v.deploy("p", "prod", lambda ver: 1 / 0))
        self.assertEqual(v.get("p", "prod").text, "good")

    def test_every_change_is_chained_into_the_audit_log(self):
        log = AuditLog()
        v = PromptVault(audit=log)
        v.commit("p", "a")
        v.tag("p", "stable")
        v.deploy("p", "prod", lambda ver: True)
        self.assertEqual([e["event"] for e in log.entries], ["prompt_commit", "prompt_tag", "prompt_deploy"])
        self.assertTrue(log.verify())


if __name__ == "__main__":
    unittest.main()
