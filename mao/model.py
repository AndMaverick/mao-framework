"""Model layer: declare what each model can do, what it costs, and how much risk it is cleared for."""

from __future__ import annotations

from dataclasses import dataclass


class NoModelError(LookupError):
    """Raised when no registered model satisfies a request."""


@dataclass(frozen=True)
class ModelSpec:
    """A model as an engineering object, not a brand.

    risk_tier is the highest task risk (1 low, 2 medium, 3 high) the model is
    cleared to handle. A model cleared for tier 2 may serve tier 1 and 2 tasks.
    """

    name: str
    capabilities: frozenset
    cost_per_1k: float
    p50_latency_ms: int
    risk_tier: int
    context_window: int = 8192

    def __post_init__(self) -> None:
        if self.risk_tier not in (1, 2, 3):
            raise ValueError("risk_tier must be 1, 2, or 3")
        if self.cost_per_1k < 0 or self.p50_latency_ms < 0:
            raise ValueError("cost and latency must be non-negative")


class ModelRegistry:
    def __init__(self, models=()) -> None:
        self._models: dict[str, ModelSpec] = {}
        for model in models:
            self.register(model)

    def register(self, model: ModelSpec) -> None:
        if model.name in self._models:
            raise ValueError(f"model already registered: {model.name}")
        self._models[model.name] = model

    def get(self, name: str) -> ModelSpec:
        try:
            return self._models[name]
        except KeyError:
            raise NoModelError(f"unknown model: {name}") from None

    def select(self, needs, *, risk: int = 1, max_cost_per_1k: float | None = None,
               max_latency_ms: int | None = None, min_context: int = 0) -> ModelSpec:
        """Return the cheapest model that meets every constraint.

        Ties on cost break on latency, then name, so selection is deterministic.
        """
        needs = frozenset(needs)
        eligible = [
            m for m in self._models.values()
            if needs <= m.capabilities
            and m.risk_tier >= risk
            and m.context_window >= min_context
            and (max_cost_per_1k is None or m.cost_per_1k <= max_cost_per_1k)
            and (max_latency_ms is None or m.p50_latency_ms <= max_latency_ms)
        ]
        if not eligible:
            raise NoModelError(
                f"no model for needs={sorted(needs)} risk={risk} "
                f"cost<={max_cost_per_1k} latency<={max_latency_ms}"
            )
        return min(eligible, key=lambda m: (m.cost_per_1k, m.p50_latency_ms, m.name))
