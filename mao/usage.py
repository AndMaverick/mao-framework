"""Token usage and exact-decimal pricing. Prices are always supplied by you, never bundled.

Token semantics match the rest of the toolchain: ``input_tokens`` is uncached input only, cached
reads and cache writes are separate counts, and none of the input counts overlap. ``output_tokens``
includes any reasoning tokens. A price that was not supplied is never assumed: metering a call that
needs it raises ``MissingPrice`` instead of quietly using the standard input rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional


class MissingPrice(LookupError):
    """A usage report needs a price that the price book does not carry."""


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_tokens"):
            _count(name, getattr(self, name))

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cached_input_tokens + self.cache_write_tokens


def _d(x: Any) -> Decimal:
    return Decimal(repr(float(x)))


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: Decimal
    output_per_million: Decimal
    cached_input_per_million: Optional[Decimal] = None
    cache_write_per_million: Optional[Decimal] = None


class PriceBook:
    """Per-million-token prices by model name, in the same shape as a Token Ledger price file."""

    def __init__(self, prices: Mapping[str, Mapping[str, Any]]) -> None:
        self._prices: Dict[str, ModelPrice] = {}
        for model, p in prices.items():
            if model.startswith("_") or model == "tier_multipliers":
                continue
            try:
                entry = ModelPrice(
                    _d(p["input_per_million"]),
                    _d(p["output_per_million"]),
                    _d(p["cached_input_per_million"]) if "cached_input_per_million" in p else None,
                    _d(p["cache_write_per_million"]) if "cache_write_per_million" in p else None,
                )
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"price entry for '{model}' needs input_per_million and output_per_million") from None
            if min(v for v in (entry.input_per_million, entry.output_per_million,
                               entry.cached_input_per_million, entry.cache_write_per_million) if v is not None) < 0:
                raise ValueError(f"price entry for '{model}' contains a negative price")
            self._prices[model] = entry

    @classmethod
    def from_json(cls, path: str) -> PriceBook:
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def __contains__(self, model: str) -> bool:
        return model in self._prices

    def cost(self, model: str, usage: Usage) -> float:
        price = self._prices.get(model)
        if price is None:
            raise MissingPrice(f"no price for model '{model}'")
        total = _d(usage.input_tokens) * price.input_per_million + _d(usage.output_tokens) * price.output_per_million
        if usage.cached_input_tokens:
            if price.cached_input_per_million is None:
                raise MissingPrice(f"model '{model}' has cached tokens but no cached_input_per_million")
            total += _d(usage.cached_input_tokens) * price.cached_input_per_million
        if usage.cache_write_tokens:
            if price.cache_write_per_million is None:
                raise MissingPrice(f"model '{model}' has cache writes but no cache_write_per_million")
            total += _d(usage.cache_write_tokens) * price.cache_write_per_million
        return float(total / Decimal(1_000_000))
