"""Orchestration layer: plans as dependency graphs, every step gated, verified, metered and logged."""

from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any, Callable, Dict, List, Optional, Tuple

from .action import Action, Policy
from .audit import AuditLog
from .model import ModelRegistry, ModelSpec, NoModelError
from .usage import MissingPrice, PriceBook, Usage


@dataclass(frozen=True)
class Step:
    """One unit of a plan.

    ``retries`` is the maximum number of attempts. A step whose action has side effects is attempted
    once unless you declare it ``idempotent``, because repeating a non-idempotent side effect after an
    ambiguous failure is how duplicate payments and duplicate messages happen.
    """

    id: str
    action: Action
    needs: frozenset = frozenset()  # type: ignore[type-arg]
    depends_on: tuple = ()  # type: ignore[type-arg]
    verify: Optional[Callable[[Any], bool]] = field(default=None, compare=False, repr=False)
    retries: int = 1
    idempotent: bool = False


@dataclass
class _State:
    spend: float = 0.0
    attempts: int = 0
    suppressed: int = 0


@dataclass
class RunResult:
    ok: bool
    outputs: Dict[str, Any]
    spend: float
    failed_step: Optional[str] = None
    reason: Optional[str] = None
    attempts: int = 0
    retries_suppressed: int = 0
    anchor: Optional[Dict[str, Any]] = None


class Orchestrator:
    """Runs a plan under a policy, a budget, and an audit log.

    ``approver(step) -> bool`` is the human-in-the-loop hook. Approval is granted only when it returns
    exactly ``True``. With no approver, an approver that raises, or any other return value, the step is
    denied: the system fails closed.

    Spend is metered per attempt. If an action reports tokens through ``ctx["report_usage"]`` and a
    ``PriceBook`` is supplied, the attempt is charged its exact cost. Otherwise it is charged the
    model's flat ``cost_per_1k`` as a unit estimate and the audit entry says so. The budget is checked
    before every attempt and again after it, so a single attempt can overshoot by at most its own cost.
    """

    def __init__(self, registry: ModelRegistry, policy: Policy, audit: Optional[AuditLog] = None,
                 approver: Optional[Callable[[Step], Any]] = None, budget: Optional[float] = None,
                 prices: Optional[PriceBook] = None, workload: str = "mao", team: str = "(unassigned)") -> None:
        self.registry = registry
        self.policy = policy
        self.audit = audit if audit is not None else AuditLog()
        self.approver = approver
        self.budget = budget
        self.prices = prices
        self.workload = workload
        self.team = team

    def run(self, steps: Any, inputs: Any = None) -> RunResult:
        steps = list(steps)
        by_id = {s.id: s for s in steps}
        if len(by_id) != len(steps):
            return self._halt({}, None, None, "duplicate step ids")
        for step in steps:
            missing = [d for d in step.depends_on if d not in by_id]
            if missing:
                return self._halt({}, None, step.id, f"unknown dependency: {missing}")
        try:
            order = list(TopologicalSorter({s.id: set(s.depends_on) for s in steps}).static_order())
        except CycleError:
            return self._halt({}, None, None, "dependency cycle")

        state = _State()
        outputs: Dict[str, Any] = {}
        self.audit.append("run.start", steps=order, budget=self.budget)
        for step_id in order:
            step = by_id[step_id]
            action = step.action
            try:
                model = self.registry.select(step.needs, risk=action.risk)
            except NoModelError as exc:
                return self._halt(outputs, state, step_id, str(exc))
            if self._over(state.spend, model.cost_per_1k):
                return self._halt(outputs, state, step_id, "budget exceeded")

            decision = self.policy.check(action)
            self.audit.append("gate", step=step_id, model=model.name, allowed=decision.allowed,
                              needs_approval=decision.needs_approval, reason=decision.reason)
            if not decision.allowed:
                return self._halt(outputs, state, step_id, decision.reason)
            if decision.needs_approval and not self._approved(step):
                return self._halt(outputs, state, step_id, "approval denied")

            ctx = {"inputs": inputs, "deps": {d: outputs[d] for d in step.depends_on}, "model": model.name}
            result, error = self._execute(step, ctx, model, state)
            if error:
                return self._halt(outputs, state, step_id, error)
            outputs[step_id] = result
            self.audit.append("step.done", step=step_id, model=model.name)

        self.audit.append("run.done", spend=state.spend, attempts=state.attempts)
        return RunResult(True, outputs, state.spend, attempts=state.attempts,
                         retries_suppressed=state.suppressed, anchor=self.audit.anchor())

    def _over(self, spend: float, next_cost: float) -> bool:
        return self.budget is not None and spend + next_cost > self.budget

    def _approved(self, step: Step) -> bool:
        error = None
        try:
            approved = self.approver(step) is True if self.approver else False
        except Exception as exc:  # an approver that fails is a denial, never a pass
            approved, error = False, f"{type(exc).__name__}: {exc}"
        fields = {"error": error} if error else {}
        self.audit.append("approval", step=step.id, approved=approved, **fields)
        return approved

    def _execute(self, step: Step, ctx: Dict[str, Any], model: ModelSpec,
                 state: _State) -> Tuple[Any, Optional[str]]:
        attempts = step.retries
        if step.action.side_effects and not step.idempotent and attempts > 1:
            state.suppressed += attempts - 1
            self.audit.append("retry.suppressed", step=step.id, requested=attempts, allowed=1)
            attempts = 1
        error = "no attempts made"
        for attempt in range(1, attempts + 1):
            if attempt > 1 and self._over(state.spend, model.cost_per_1k):
                return None, "budget exceeded"
            reported: List[Usage] = []
            attempt_ctx = {**ctx, "report_usage": reported.append}
            state.attempts += 1
            result: Any = None
            outcome = "ok"
            try:
                result = step.action.fn(attempt_ctx)
            except Exception as exc:  # a step may fail in any way; the log records it
                error = f"{type(exc).__name__}: {exc}"
                outcome = "error"
                self.audit.append("attempt.error", step=step.id, attempt=attempt, error=error)
            if outcome == "ok" and step.verify is not None:
                try:
                    verified = bool(step.verify(result))
                    detail = None
                except Exception as exc:
                    verified, detail = False, f"{type(exc).__name__}: {exc}"
                if not verified:
                    error, outcome = "verification failed", "unverified"
                    extra = {"error": detail} if detail else {}
                    self.audit.append("attempt.unverified", step=step.id, attempt=attempt, **extra)
            problem = self._meter(step, model, attempt, reported, outcome, state)
            if problem:
                return None, problem
            if outcome == "ok":
                return result, None
        return None, error

    def _meter(self, step: Step, model: ModelSpec, attempt: int, reported: List[Usage],
               outcome: str, state: _State) -> Optional[str]:
        usage = Usage()
        for u in reported:
            usage = usage + u
        estimated = not (reported and self.prices is not None)
        if estimated:
            cost = model.cost_per_1k
        else:
            try:
                assert self.prices is not None
                cost = self.prices.cost(model.name, usage)
            except MissingPrice as exc:
                self.audit.append("usage.unpriced", step=step.id, attempt=attempt, model=model.name, reason=str(exc))
                return f"cannot meter spend: {exc}"
        state.spend += cost
        self.audit.append(
            "usage", step=step.id, attempt=attempt, model=model.name, provider=model.provider,
            workload=self.workload, team=self.team, outcome=outcome, estimated=estimated, cost=cost,
            input_tokens=usage.input_tokens, cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens, output_tokens=usage.output_tokens,
        )
        if self.budget is not None and state.spend > self.budget:
            return "budget exceeded"
        return None

    def _halt(self, outputs: Dict[str, Any], state: Optional[_State], step_id: Optional[str], reason: str) -> RunResult:
        state = state or _State()
        self.audit.append("run.halt", step=step_id, reason=reason, spend=state.spend)
        return RunResult(False, outputs, state.spend, step_id, reason, state.attempts, state.suppressed, self.audit.anchor())
