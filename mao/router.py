"""A learning router: pick among models by upper confidence bound on net reward.

``ModelRegistry.select`` answers "which models are allowed to do this, and which is cheapest". It is
deterministic and governed. ``Router`` answers a different question: among models that are all allowed,
which one has earned its cost on your traffic. Feed it the quality you measured and the cost you metered,
and it shifts traffic toward the model whose quality gap pays for its price. Use the registry to decide
what is permitted and the router to choose among the permitted. Deterministic: ties break by arm order.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional


class Router:
    """UCB1 over models. Reward for one call is ``quality - cost_weight * cost``."""

    def __init__(self, arms: Iterable[str], exploration: float = 1.0, cost_weight: float = 0.0) -> None:
        self.arms: List[str] = list(arms)
        if not self.arms:
            raise ValueError("need at least one arm")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("arms must be unique")
        if exploration < 0 or cost_weight < 0:
            raise ValueError("exploration and cost_weight must be non-negative")
        self.exploration = exploration
        self.cost_weight = cost_weight
        self.pulls: Dict[str, int] = dict.fromkeys(self.arms, 0)
        self.totals: Dict[str, float] = dict.fromkeys(self.arms, 0.0)
        self.rounds = 0

    def select(self, allowed: Optional[Iterable[str]] = None) -> str:
        """Choose an arm. ``allowed`` restricts the choice, for example to what the policy permits."""
        pool = self.arms if allowed is None else [a for a in self.arms if a in set(allowed)]
        if not pool:
            raise ValueError("no allowed arm")
        for arm in pool:
            if self.pulls[arm] == 0:
                return arm

        def ucb(arm: str) -> float:
            mean = self.totals[arm] / self.pulls[arm]
            return mean + self.exploration * math.sqrt(math.log(max(self.rounds, 2)) / self.pulls[arm])

        return max(pool, key=ucb)

    def update(self, arm: str, quality: float, cost: float = 0.0) -> float:
        if arm not in self.pulls:
            raise ValueError(f"unknown arm: {arm}")
        reward = quality - self.cost_weight * cost
        self.pulls[arm] += 1
        self.totals[arm] += reward
        self.rounds += 1
        return reward

    def means(self) -> Dict[str, Optional[float]]:
        return {a: (self.totals[a] / self.pulls[a] if self.pulls[a] else None) for a in self.arms}

    def best(self) -> str:
        tried = {a: m for a, m in self.means().items() if m is not None}
        if not tried:
            raise ValueError("no arm has been tried")
        return max(tried, key=lambda a: tried[a])
