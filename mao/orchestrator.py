"""Orchestration layer: plans as dependency graphs, every step gated, verified, and logged."""

from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Callable

from .action import Action, Policy
from .audit import AuditLog
from .model import ModelRegistry, NoModelError


@dataclass(frozen=True)
class Step:
    id: str
    action: Action
    needs: frozenset = frozenset()
    depends_on: tuple = ()
    verify: Callable | None = field(default=None, compare=False, repr=False)
    retries: int = 1


@dataclass
class RunResult:
    ok: bool
    outputs: dict
    spend: float
    failed_step: str | None = None
    reason: str | None = None


class Orchestrator:
    """Runs a plan under a policy, a budget, and an audit log.

    approver(step) -> bool is the human-in-the-loop hook. With no approver,
    any step that needs approval is denied. Failing closed is the default.
    """

    def __init__(self, registry: ModelRegistry, policy: Policy, audit: AuditLog | None = None,
                 approver: Callable | None = None, budget: float | None = None) -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit if audit is not None else AuditLog()
        self.approver = approver
        self.budget = budget

    def run(self, steps, inputs=None) -> RunResult:
        steps = list(steps)
        by_id = {s.id: s for s in steps}
        if len(by_id) != len(steps):
            return self._halt({}, 0.0, None, "duplicate step ids")
        for step in steps:
            missing = [d for d in step.depends_on if d not in by_id]
            if missing:
                return self._halt({}, 0.0, step.id, f"unknown dependency: {missing}")
        try:
            order = list(TopologicalSorter({s.id: set(s.depends_on) for s in steps}).static_order())
        except CycleError:
            return self._halt({}, 0.0, None, "dependency cycle")

        outputs: dict = {}
        spend = 0.0
        self.audit.append("run.start", steps=order)
        for step_id in order:
            step = by_id[step_id]
            action = step.action
            try:
                model = self.registry.select(step.needs, risk=action.risk)
            except NoModelError as exc:
                return self._halt(outputs, spend, step_id, str(exc))
            if self.budget is not None and spend + model.cost_per_1k > self.budget:
                return self._halt(outputs, spend, step_id, "budget exceeded")

            decision = self.policy.check(action)
            self.audit.append("gate", step=step_id, model=model.name, allowed=decision.allowed,
                              needs_approval=decision.needs_approval, reason=decision.reason)
            if not decision.allowed:
                return self._halt(outputs, spend, step_id, decision.reason)
            if decision.needs_approval:
                approved = bool(self.approver(step)) if self.approver else False
                self.audit.append("approval", step=step_id, approved=approved)
                if not approved:
                    return self._halt(outputs, spend, step_id, "approval denied")

            ctx = {"inputs": inputs, "deps": {d: outputs[d] for d in step.depends_on},
                   "model": model.name}
            result, error = self._execute(step, ctx)
            spend += model.cost_per_1k
            if error:
                return self._halt(outputs, spend, step_id, error)
            outputs[step_id] = result
            self.audit.append("step.done", step=step_id, model=model.name)

        self.audit.append("run.done", spend=spend)
        return RunResult(True, outputs, spend)

    def _execute(self, step: Step, ctx: dict):
        error = "no attempts made"
        for attempt in range(1, step.retries + 1):
            try:
                result = step.action.fn(ctx)
            except Exception as exc:  # a step may fail in any way; the log records it
                error = f"{type(exc).__name__}: {exc}"
                self.audit.append("attempt.error", step=step.id, attempt=attempt, error=error)
                continue
            if step.verify is not None and not step.verify(result):
                error = "verification failed"
                self.audit.append("attempt.unverified", step=step.id, attempt=attempt)
                continue
            return result, None
        return None, error

    def _halt(self, outputs, spend, step_id, reason) -> RunResult:
        self.audit.append("run.halt", step=step_id, reason=reason)
        return RunResult(False, outputs, spend, step_id, reason)
