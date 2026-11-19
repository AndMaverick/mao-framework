"""Plans as data, and playbooks as markdown.

A ``Plan`` is a dependency graph you can validate, order, cost and time before anything runs, and then
audit against a ``Policy`` so a declared approval gate that the policy would not enforce, or an
approval the plan forgot to declare, is found on paper and not in production. ``compile_playbook``
turns a human-written markdown playbook into the same structure and lints it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Any, Dict, FrozenSet, List, Mapping, Tuple

from .action import Action, Policy
from .orchestrator import Step


class SpecError(ValueError):
    """The plan or playbook is invalid."""


@dataclass(frozen=True)
class StepSpec:
    id: str
    depends_on: Tuple[str, ...] = ()
    needs: FrozenSet[str] = frozenset()
    cost: float = 0.0
    latency: float = 0.0
    approval: bool = False


class Plan:
    def __init__(self, steps: Dict[str, StepSpec]) -> None:
        self.steps = steps

    def order(self) -> List[str]:
        graph = {s.id: set(s.depends_on) for s in self.steps.values()}
        return list(TopologicalSorter(graph).static_order())

    def levels(self) -> List[List[str]]:
        """Groups of steps that can run in parallel, in execution order."""
        depth: Dict[str, int] = {}
        for step_id in self.order():
            depth[step_id] = 1 + max((depth[d] for d in self.steps[step_id].depends_on), default=-1)
        grouped: Dict[int, List[str]] = {}
        for step_id, level in depth.items():
            grouped.setdefault(level, []).append(step_id)
        return [sorted(grouped[k]) for k in sorted(grouped)]

    def total_cost(self) -> float:
        return round(sum(s.cost for s in self.steps.values()), 6)

    def critical_path(self) -> Tuple[List[str], float]:
        """The longest chain by latency, which bounds the run's wall-clock time however much runs in parallel."""
        best: Dict[str, Tuple[List[str], float]] = {}
        for step_id in self.order():
            step = self.steps[step_id]
            prior: Tuple[List[str], float] = max((best[d] for d in step.depends_on), key=lambda r: r[1], default=([], 0.0))
            best[step_id] = (prior[0] + [step_id], prior[1] + step.latency)
        return max(best.values(), key=lambda r: r[1])

    def approvals(self) -> List[str]:
        return [i for i in self.order() if self.steps[i].approval]

    def to_steps(self, actions: Mapping[str, Action]) -> List[Step]:
        """Bind each step to the action that runs it. A step with no action is an error, never skipped."""
        missing = [i for i in self.order() if i not in actions]
        if missing:
            raise SpecError(f"no action bound for step(s): {', '.join(missing)}")
        return [Step(i, actions[i], needs=self.steps[i].needs, depends_on=self.steps[i].depends_on) for i in self.order()]

    def audit(self, actions: Mapping[str, Action], policy: Policy) -> List[str]:
        """Compare what the plan declares with what the policy would enforce. An empty list means they agree."""
        findings: List[str] = []
        for step_id in self.order():
            action = actions.get(step_id)
            if action is None:
                findings.append(f"{step_id}: no action bound")
                continue
            decision = policy.check(action)
            declared = self.steps[step_id].approval
            if not decision.allowed:
                findings.append(f"{step_id}: policy denies it ({decision.reason})")
            elif decision.needs_approval and not declared:
                findings.append(f"{step_id}: policy will require approval but the plan does not declare it")
            elif declared and not decision.needs_approval:
                findings.append(f"{step_id}: plan declares an approval gate that the policy will not enforce")
        return findings


def load_plan(spec: Mapping[str, Any]) -> Plan:
    raw = spec.get("steps") if isinstance(spec, Mapping) else None
    if not raw:
        raise SpecError("spec needs a non-empty 'steps' list")
    steps: Dict[str, StepSpec] = {}
    for item in raw:
        step_id = item.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise SpecError("every step needs a string id")
        if step_id in steps:
            raise SpecError(f"duplicate step id: {step_id}")
        cost, latency = float(item.get("cost", 0.0)), float(item.get("latency", 0.0))
        if cost < 0 or latency < 0 or cost != cost or latency != latency:
            raise SpecError(f"{step_id}: cost and latency must be non-negative numbers")
        steps[step_id] = StepSpec(step_id, tuple(item.get("depends_on", ())), frozenset(item.get("needs", ())),
                                  cost, latency, bool(item.get("approval", False)))
    for step in steps.values():
        for dep in step.depends_on:
            if dep not in steps:
                raise SpecError(f"{step.id} depends on unknown step: {dep}")
    plan = Plan(steps)
    try:
        plan.order()
    except CycleError:
        raise SpecError("plan contains a dependency cycle") from None
    return plan


_STEP = re.compile(r"^##\s+Step\s+(\d+):\s*(.+?)\s*$")
_TASK = re.compile(r"^[-*]\s+\[( |x|X)\]\s+(.*)$")


def compile_playbook(text: str) -> Dict[str, Any]:
    """Parse a markdown playbook: optional front matter, a ``# Title``, then ``## Step N: name`` sections of checklists."""
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                for raw in lines[1:index]:
                    if ":" in raw:
                        key, value = raw.split(":", 1)
                        meta[key.strip().lower()] = value.strip()
                lines = lines[index + 1:]
                break
    title = None
    steps: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for raw in lines:
        line = raw.strip()
        if title is None and line.startswith("# "):
            title = line[2:].strip()
            continue
        step = _STEP.match(line)
        if step:
            current = {"number": int(step.group(1)), "title": step.group(2), "tasks": [], "notes": []}
            steps.append(current)
            continue
        if not current or not line:
            continue
        task = _TASK.match(line)
        if task:
            current["tasks"].append({"text": task.group(2).strip(), "done": task.group(1) != " "})
        else:
            current["notes"].append(line)
    return {"title": title, "meta": meta, "steps": steps}


def lint_playbook(playbook: Mapping[str, Any]) -> List[str]:
    issues: List[str] = []
    if not playbook["title"]:
        issues.append("missing title (a single '# Title' line)")
    for key in ("owner", "version"):
        if key not in playbook["meta"]:
            issues.append(f"missing front matter: {key}")
    steps = playbook["steps"]
    if not steps:
        issues.append("no steps found")
    for expected, step in enumerate(steps, start=1):
        if step["number"] != expected:
            issues.append(f"step numbering: expected {expected}, found {step['number']}")
        if not step["tasks"]:
            issues.append(f"step {step['number']} has no checklist items")
        if any(not t["text"] for t in step["tasks"]):
            issues.append(f"step {step['number']} has an empty checklist item")
    return issues


def playbook_progress(playbook: Mapping[str, Any]) -> Tuple[int, int]:
    """(done, total) across every checklist item."""
    tasks = [t for s in playbook["steps"] for t in s["tasks"]]
    return sum(1 for t in tasks if t["done"]), len(tasks)


def playbook_to_spec(playbook: Mapping[str, Any]) -> Dict[str, Any]:
    """A playbook as a sequential plan spec (step-1, step-2, ...), ready for ``load_plan``."""
    steps = []
    for index, _step in enumerate(playbook["steps"], start=1):
        item: Dict[str, Any] = {"id": f"step-{index}"}
        if index > 1:
            item["depends_on"] = [f"step-{index - 1}"]
        steps.append(item)
    return {"steps": steps}
