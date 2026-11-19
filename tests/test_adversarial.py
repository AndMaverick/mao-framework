"""Failure-oriented tests. Each one states the failure it guards against and why it matters."""

import json
import unittest

from mao import Action, AuditLog, ModelRegistry, ModelSpec, Orchestrator, Policy, Step

X = frozenset({"x"})


def registry(cost=1.0):
    return ModelRegistry([ModelSpec("m", X, cost, 10, 2)])


def policy(**kw):
    return Policy("role", frozenset({"r", "w"}), **kw)


def step(sid="s", fn=None, side=False, risk=1, scope="r", **kw):
    return Step(sid, Action(sid, scope, risk, side, fn or (lambda c: "ok")), needs=X, **kw)


class ApprovalGate(unittest.TestCase):
    """An approval control that can be bypassed by an error or a sloppy return value is not a control."""

    def run_with(self, approver):
        audit = AuditLog()
        ran = []
        s = step("pay", fn=lambda c: ran.append(1) or "paid", side=True, risk=2, scope="w")
        result = Orchestrator(registry(), policy(approval_risk=2), audit, approver=approver).run([s])
        return result, audit, ran

    def test_no_approver_denies(self):
        result, _, ran = self.run_with(None)
        self.assertFalse(result.ok)
        self.assertEqual((result.reason, ran), ("approval denied", []))

    def test_approver_that_raises_denies_and_is_logged(self):
        result, audit, ran = self.run_with(lambda s: 1 / 0)
        self.assertEqual((result.ok, ran), (False, []))
        approval = next(e for e in audit.entries if e["event"] == "approval")
        self.assertFalse(approval["approved"])
        self.assertIn("ZeroDivisionError", approval["error"])
        self.assertTrue(audit.verify())

    def test_truthy_but_not_true_is_denied(self):
        for sloppy in ("no", "yes", 1, [0], object()):
            with self.subTest(sloppy=sloppy):
                result, _, ran = self.run_with(lambda s, v=sloppy: v)
                self.assertEqual((result.ok, ran), (False, []))

    def test_only_true_approves(self):
        result, _, ran = self.run_with(lambda s: True)
        self.assertEqual((result.ok, ran), (True, [1]))

    def test_denied_step_never_reaches_downstream(self):
        audit = AuditLog()
        down = []
        steps = [step("pay", side=True, risk=2, scope="w"),
                 step("notify", fn=lambda c: down.append(1), depends_on=("pay",))]
        result = Orchestrator(registry(), policy(approval_risk=2), audit, approver=lambda s: False).run(steps)
        self.assertEqual((result.ok, down, result.failed_step), (False, [], "pay"))


class Verification(unittest.TestCase):
    def test_verifier_that_raises_is_a_failed_verification_not_a_crash(self):
        audit = AuditLog()
        s = step(fn=lambda c: None, verify=lambda out: out["k"])
        result = Orchestrator(registry(), policy(), audit).run([s])
        self.assertEqual((result.ok, result.reason), (False, "verification failed"))
        entry = next(e for e in audit.entries if e["event"] == "attempt.unverified")
        self.assertIn("TypeError", entry["error"])

    def test_unverified_output_never_reaches_a_dependent_step(self):
        seen = []
        steps = [step("a", fn=lambda c: "bad", verify=lambda o: o == "good"),
                 step("b", fn=lambda c: seen.append(c["deps"]), depends_on=("a",))]
        result = Orchestrator(registry(), policy(), AuditLog()).run(steps)
        self.assertEqual((result.ok, seen), (False, []))

    def test_retry_recovers_when_a_later_attempt_verifies(self):
        calls = []
        s = step(fn=lambda c: calls.append(1) or ("good" if len(calls) == 3 else "bad"),
                 verify=lambda o: o == "good", retries=3)
        result = Orchestrator(registry(), policy(), AuditLog()).run([s])
        self.assertEqual((result.ok, len(calls), result.attempts), (True, 3, 3))


class Retries(unittest.TestCase):
    def test_every_attempt_is_charged(self):
        calls = []

        def flaky(c):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("transient")
            return "ok"
        result = Orchestrator(registry(cost=2.0), policy(), AuditLog(), budget=100).run([step(fn=flaky, retries=3)])
        self.assertEqual((result.ok, result.spend), (True, 6.0))

    def test_side_effect_is_never_retried_unless_declared_idempotent(self):
        calls = []

        def post_then_fail(c):
            calls.append(1)
            raise TimeoutError("committed but the reply was lost")
        audit = AuditLog()
        s = step(fn=post_then_fail, side=True, retries=3)
        result = Orchestrator(registry(), policy(approval_risk=3), audit).run([s])
        self.assertEqual((result.ok, len(calls), result.retries_suppressed), (False, 1, 2))
        self.assertTrue(any(e["event"] == "retry.suppressed" for e in audit.entries))

    def test_declared_idempotent_side_effect_may_retry(self):
        calls = []
        s = step(fn=lambda c: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")) if len(calls) < 2 else "ok",
                 side=True, retries=3, idempotent=True)
        result = Orchestrator(registry(), policy(approval_risk=3), AuditLog()).run([s])
        self.assertEqual((result.ok, len(calls)), (True, 2))

    def test_zero_attempts_halts_cleanly(self):
        result = Orchestrator(registry(), policy(), AuditLog()).run([step(retries=0)])
        self.assertEqual((result.ok, result.reason), (False, "no attempts made"))


class Budget(unittest.TestCase):
    def test_budget_stops_a_runaway_retry_loop(self):
        calls = []
        s = step(fn=lambda c: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")), retries=1000)
        result = Orchestrator(registry(cost=1.0), policy(), AuditLog(), budget=5.0).run([s])
        self.assertEqual((result.ok, result.reason), (False, "budget exceeded"))
        self.assertLessEqual(len(calls), 5)
        self.assertLessEqual(result.spend, 5.0)

    def test_budget_is_inclusive_at_the_limit(self):
        steps = [step("a"), step("b", depends_on=("a",))]
        self.assertTrue(Orchestrator(registry(cost=1.0), policy(), AuditLog(), budget=2.0).run(steps).ok)
        self.assertFalse(Orchestrator(registry(cost=1.0), policy(), AuditLog(), budget=1.99).run(steps).ok)

    def test_spend_reported_on_a_halted_run(self):
        steps = [step("a"), step("b", depends_on=("a",))]
        result = Orchestrator(registry(cost=1.0), policy(), AuditLog(), budget=1.0).run(steps)
        self.assertEqual((result.ok, result.spend, result.failed_step), (False, 1.0, "b"))


class PlanValidation(unittest.TestCase):
    def test_cycle_unknown_dependency_and_duplicate_ids_halt_before_any_action_runs(self):
        ran = []
        fn = lambda c: ran.append(1)  # noqa: E731
        cases = {
            "cycle": [step("a", fn=fn, depends_on=("b",)), step("b", fn=fn, depends_on=("a",))],
            "unknown": [step("a", fn=fn, depends_on=("ghost",))],
            "duplicate": [step("a", fn=fn), step("a", fn=fn)],
        }
        for name, steps in cases.items():
            with self.subTest(name):
                result = Orchestrator(registry(), policy(), AuditLog()).run(steps)
                self.assertFalse(result.ok)
        self.assertEqual(ran, [])

    def test_scope_and_risk_are_enforced(self):
        ran = []
        s = step(fn=lambda c: ran.append(1), scope="admin")
        self.assertFalse(Orchestrator(registry(), policy(), AuditLog()).run([s]).ok)
        s = step(fn=lambda c: ran.append(1), risk=3)
        self.assertFalse(Orchestrator(registry(), policy(max_risk=2), AuditLog()).run([s]).ok)
        self.assertEqual(ran, [])


class AuditIntegrity(unittest.TestCase):
    def make(self, n=6):
        log = AuditLog(clock=lambda: 1_700_000_000.0)
        for i in range(n):
            log.append("e", i=i)
        return log

    def test_edit_in_the_middle_is_detected_and_located(self):
        log = self.make()
        log.entries[2]["i"] = 999
        result = log.check()
        self.assertEqual((result.ok, result.index), (False, 2))

    def test_deletion_from_the_middle_is_detected(self):
        log = self.make()
        del log.entries[2]
        self.assertFalse(log.verify())

    def test_reordering_is_detected(self):
        log = self.make()
        log.entries[1], log.entries[2] = log.entries[2], log.entries[1]
        self.assertFalse(log.verify())

    def test_truncation_is_detected_only_with_an_anchor(self):
        log = self.make()
        anchor = log.anchor()
        log.entries = log.entries[:3]
        self.assertTrue(log.verify(), "a bare chain cannot see a cut tail; this is the documented limit")
        self.assertFalse(log.verify(length=anchor["length"]))
        self.assertFalse(log.verify(head=anchor["head"]))

    def test_full_rewrite_is_detected_only_with_an_anchor(self):
        honest = self.make()
        anchor = honest.anchor()
        forged = AuditLog(clock=lambda: 1_700_000_000.0)
        for i in range(6):
            forged.append("e", i=i if i != 3 else 12345)
        self.assertTrue(forged.verify())
        self.assertFalse(forged.verify(head=anchor["head"], length=anchor["length"]))
        self.assertTrue(honest.verify(head=anchor["head"], length=anchor["length"]))

    def test_reserved_field_names_cannot_corrupt_the_chain(self):
        log = AuditLog()
        for name in ("seq", "ts", "event", "prev", "hash"):
            with self.assertRaises((ValueError, TypeError)):  # "event" is rejected by Python itself
                log.append("e", **{name: 1})
        self.assertEqual(len(log.entries), 0)

    def test_roundtrip_through_jsonl_still_verifies(self):
        audit = AuditLog()
        Orchestrator(registry(), policy(), audit).run([step("a"), step("b", depends_on=("a",))])
        again = AuditLog.from_jsonl(audit.to_jsonl())
        self.assertTrue(again.verify(**audit.anchor()))

    def test_a_tampered_exported_file_fails_after_reload(self):
        audit = AuditLog()
        Orchestrator(registry(), policy(), audit).run([step("a")])
        lines = audit.to_jsonl().splitlines()
        entry = json.loads(lines[1])
        entry["reason"] = "edited"
        lines[1] = json.dumps(entry, sort_keys=True)
        self.assertFalse(AuditLog.from_jsonl("\n".join(lines)).verify())

    def test_malformed_entries_fail_verification_instead_of_raising(self):
        log = self.make(3)
        del log.entries[1]["hash"]
        self.assertFalse(log.verify())
        with self.assertRaises(ValueError):
            AuditLog.from_jsonl("not json")

    def test_every_decision_is_on_the_record(self):
        audit = AuditLog()
        s = step("pay", side=True, risk=2, scope="w")
        Orchestrator(registry(), policy(approval_risk=2), audit, approver=lambda x: False).run([s])
        self.assertEqual([e["event"] for e in audit.entries], ["run.start", "gate", "approval", "run.halt"])


if __name__ == "__main__":
    unittest.main()
