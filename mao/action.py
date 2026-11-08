"""Action layer: what a system is allowed to do, decided before it does it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Action:
    """A unit of capability with an explicit blast radius."""

    name: str
    scope: str
    risk: int
    side_effects: bool
    fn: Callable = field(compare=False, repr=False, default=lambda inputs: inputs)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    needs_approval: bool
    reason: str


@dataclass
class Policy:
    """Grants for one role.

    allowed_scopes: scopes the role may touch at all.
    max_risk: highest action risk the role may run.
    approval_risk: side-effecting actions at or above this risk need a human.
    """

    role: str
    allowed_scopes: frozenset
    max_risk: int = 2
    approval_risk: int = 2

    def check(self, action: Action) -> Decision:
        if action.scope not in self.allowed_scopes:
            return Decision(False, False, f"scope '{action.scope}' not granted to {self.role}")
        if action.risk > self.max_risk:
            return Decision(False, False, f"risk {action.risk} exceeds ceiling {self.max_risk}")
        if action.side_effects and action.risk >= self.approval_risk:
            return Decision(True, True, "side effect at or above approval threshold")
        return Decision(True, False, "within grant")
