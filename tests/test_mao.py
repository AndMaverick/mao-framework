import unittest

from mao import (Action, AuditLog, ModelRegistry, ModelSpec, NoModelError, Orchestrator,
                 Policy, Step, score)


def registry():
    return ModelRegistry([
        ModelSpec("small", frozenset({"extract"}), 0.2, 120, 1),
        ModelSpec("large", frozenset({"extract", "draft"}), 3.0, 900, 2),
        ModelSpec("frontier", frozenset({"extract", "draft", "reason"}), 15.0, 2500, 3),
    ])


def policy(**kw):
    return Policy("analyst", frozenset({"read", "write"}), **kw)


def act(name, scope="read", risk=1, side=False, fn=None):
    return Action(name, scope, risk, side, fn or (lambda c: name))


class ModelTests(unittest.TestCase):
    def test_cheapest_capable_model_wins(self):
        self.assertEqual(registry().select({"extract"}).name, "small")
        self.assertEqual(registry().select({"draft"}).name, "large")

    def test_risk_raises_the_floor(self):
        self.assertEqual(registry().select({"extract"}, risk=3).name, "frontier")

    def test_constraints_can_make_selection_impossible(self):
        with self.assertRaises(NoModelError):
            registry().select({"reason"}, max_cost_per_1k=5.0)

    def test_duplicate_registration_rejected(self):
        with self.assertRaises(ValueError):
            registry().register(ModelSpec("small", frozenset(), 1, 1, 1))


class PolicyTests(unittest.TestCase):
    def test_scope_denied(self):
        self.assertFalse(policy().check(act("x", scope="admin")).allowed)

    def test_risk_ceiling(self):
        self.assertFalse(policy(max_risk=1).check(act("x", risk=2)).allowed)

    def test_side_effect_needs_approval(self):
        d = policy().check(act("x", scope="write", risk=2, side=True))
        self.assertTrue(d.allowed and d.needs_approval)

    def test_read_is_free(self):
        d = policy().check(act("x"))
        self.assertTrue(d.allowed and not d.needs_approval)


class AuditTests(unittest.TestCase):
    def test_chain_verifies(self):
        log = AuditLog()
        log.append("a", x=1)
        log.append("b", x=2)
        self.assertTrue(log.verify())

    def test_tampering_is_detected(self):
        log = AuditLog()
        log.append("a", x=1)
        log.append("b", x=2)
        log.entries[0]["x"] = 99
        self.assertFalse(log.verify())

    def test_deletion_is_detected(self):
        log = AuditLog()
        for i in range(3):
            log.append("e", i=i)
        del log.entries[1]
        self.assertFalse(log.verify())


class OrchestratorTests(unittest.TestCase):
    def plan(self, fn=None, verify=None, retries=1):
        return [
            Step("a", act("a", fn=lambda c: 2), needs=frozenset({"extract"})),
            Step("b", act("b", fn=fn or (lambda c: c["deps"]["a"] * 10)), needs=frozenset({"draft"}),
                 depends_on=("a",), verify=verify, retries=retries),
        ]

    def test_dependencies_flow_and_chain_is_valid(self):
        orch = Orchestrator(registry(), policy())
        result = orch.run(self.plan())
        self.assertTrue(result.ok)
        self.assertEqual(result.outputs["b"], 20)
        self.assertTrue(orch.audit.verify())

    def test_approval_fails_closed(self):
        steps = [Step("p", act("p", scope="write", risk=2, side=True), needs=frozenset({"draft"}))]
        result = Orchestrator(registry(), policy()).run(steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "approval denied")

    def test_approval_granted(self):
        steps = [Step("p", act("p", scope="write", risk=2, side=True), needs=frozenset({"draft"}))]
        self.assertTrue(Orchestrator(registry(), policy(), approver=lambda s: True).run(steps).ok)

    def test_failed_verification_halts_downstream(self):
        result = Orchestrator(registry(), policy()).run(self.plan(verify=lambda out: False))
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "b")
        self.assertEqual(result.reason, "verification failed")

    def test_retry_recovers_from_transient_failure(self):
        calls = {"n": 0}

        def flaky(c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return "ok"

        result = Orchestrator(registry(), policy()).run(self.plan(fn=flaky, retries=2))
        self.assertTrue(result.ok)
        self.assertEqual(calls["n"], 2)

    def test_budget_is_enforced(self):
        result = Orchestrator(registry(), policy(), budget=1.0).run(self.plan())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "budget exceeded")

    def test_cycle_is_rejected(self):
        steps = [Step("a", act("a"), depends_on=("b",)), Step("b", act("b"), depends_on=("a",))]
        self.assertEqual(Orchestrator(registry(), policy()).run(steps).reason, "dependency cycle")

    def test_unknown_dependency_is_rejected(self):
        steps = [Step("a", act("a"), depends_on=("ghost",))]
        self.assertIn("unknown dependency", Orchestrator(registry(), policy()).run(steps).reason)


class DiagnosticTests(unittest.TestCase):
    def test_empty_scores_zero(self):
        self.assertEqual(score({})["overall"], 0)

    def test_weakest_layer_identified(self):
        answers = {q: True for q in ("m1", "m2", "m3", "m4", "o1", "o2", "o3", "o4")}
        result = score(answers)
        self.assertEqual(result["weakest"], "action")
        self.assertEqual(result["layers"]["model"]["score"], 100)


if __name__ == "__main__":
    unittest.main()
