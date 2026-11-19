"""Span trees, cost rollups, the orchestrator's tracer hook, and the learning router."""

import unittest

from mao import Action, ModelRegistry, ModelSpec, Orchestrator, Policy, Router, Step, Tracer

X = frozenset({"x"})


def ticking():
    state = {"t": 0.0}

    def clock():
        state["t"] += 1.0
        return state["t"]

    return clock


class Spans(unittest.TestCase):
    def test_tree_cost_rollup_and_render(self):
        tr = Tracer(clock=ticking())
        with tr.span("run"):
            with tr.span("a", cost=0.25):
                pass
            with tr.span("b", cost=0.5), tr.span("b1", cost=0.125):
                pass
        self.assertEqual(tr.total_cost(), 0.875)
        run = tr.spans[0]
        self.assertEqual(tr.total_cost(run.id), 0.875)
        self.assertEqual([s.name for s in tr.children(run.id)], ["a", "b"])
        self.assertIn("  b1", tr.render())

    def test_critical_path_follows_the_longest_child(self):
        tr = Tracer(clock=ticking())
        with tr.span("run"):
            with tr.span("quick"):
                pass
            with tr.span("slow"), tr.span("inner"):
                pass
        self.assertEqual([s.name for s in tr.critical_path()], ["run", "slow", "inner"])
        self.assertEqual(Tracer().critical_path(), [])

    def test_errors_are_recorded_and_reraised(self):
        tr = Tracer(clock=ticking())
        with self.assertRaises(KeyError), tr.span("boom"):
            raise KeyError("k")
        self.assertEqual(len(tr.errors()), 1)
        self.assertTrue(tr.errors()[0].error.startswith("KeyError"))
        self.assertIsNotNone(tr.spans[0].end)
        self.assertEqual(tr.to_dicts()[0]["name"], "boom")


class OrchestratorHook(unittest.TestCase):
    def build(self, fn, tracer):
        reg = ModelRegistry([ModelSpec("m", X, 0.0, 10, 1)])
        steps = [Step("one", Action("one", "r", 1, False, lambda c: 1), needs=X),
                 Step("two", Action("two", "r", 1, False, fn), needs=X, depends_on=("one",))]
        return Orchestrator(reg, Policy("p", frozenset({"r"})), tracer=tracer).run(steps)

    def test_a_run_is_a_root_span_with_one_child_per_step(self):
        tr = Tracer()
        self.assertTrue(self.build(lambda c: 2, tr).ok)
        root = tr.children(None)
        self.assertEqual([s.name for s in root], ["run"])
        self.assertEqual([s.name for s in tr.children(root[0].id)], ["one", "two"])
        self.assertEqual(tr.children(root[0].id)[0].attrs["model"], "m")

    def test_a_failed_run_marks_the_root_span(self):
        def bad(c):
            raise RuntimeError("no")

        tr = Tracer()
        self.assertFalse(self.build(bad, tr).ok)
        self.assertIsNotNone(tr.children(None)[0].error)
        self.assertTrue(tr.errors())

    def test_no_tracer_changes_nothing(self):
        self.assertTrue(self.build(lambda c: 2, None).ok)


class Routing(unittest.TestCase):
    def test_every_arm_is_tried_before_any_is_repeated(self):
        r = Router(["a", "b", "c"])
        seen = []
        for _ in range(3):
            arm = r.select()
            seen.append(arm)
            r.update(arm, 0.5)
        self.assertEqual(seen, ["a", "b", "c"])

    def test_traffic_moves_to_the_arm_that_earns_its_price(self):
        r = Router(["cheap", "premium"], exploration=0.2, cost_weight=1.0)
        quality = {"cheap": 0.80, "premium": 0.85}
        cost = {"cheap": 0.01, "premium": 0.30}  # a five point gain does not pay for thirty times the cost
        for _ in range(200):
            arm = r.select()
            r.update(arm, quality[arm], cost[arm])
        self.assertEqual(r.best(), "cheap")
        self.assertGreater(r.pulls["cheap"], r.pulls["premium"] * 3)

    def test_when_quality_pays_for_cost_the_dearer_arm_wins(self):
        r = Router(["cheap", "premium"], exploration=0.2, cost_weight=1.0)
        quality = {"cheap": 0.40, "premium": 0.95}
        cost = {"cheap": 0.01, "premium": 0.10}
        for _ in range(200):
            arm = r.select()
            r.update(arm, quality[arm], cost[arm])
        self.assertEqual(r.best(), "premium")

    def test_allowed_restricts_the_choice_and_validation(self):
        r = Router(["a", "b"])
        self.assertEqual(r.select(allowed=["b"]), "b")
        with self.assertRaises(ValueError):
            r.select(allowed=["zzz"])
        with self.assertRaises(ValueError):
            r.update("zzz", 1.0)
        with self.assertRaises(ValueError):
            Router([])
        with self.assertRaises(ValueError):
            Router(["a", "a"])
        with self.assertRaises(ValueError):
            Router(["a"]).best()

    def test_selection_is_deterministic(self):
        def run():
            r = Router(["a", "b", "c"])
            out = []
            for i in range(30):
                arm = r.select()
                r.update(arm, (i % 3) / 3)
                out.append(arm)
            return out

        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
