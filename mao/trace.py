"""Span trees for governed runs: timing, cost attached where it was incurred, rollups and a critical path.

No collector and no vendor. The orchestrator opens a span per step when you hand it a ``Tracer``, so a run
can be rendered, summed and exported without any other infrastructure.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional


@dataclass
class Span:
    id: int
    parent: Optional[int]
    name: str
    start: float
    end: Optional[float] = None
    cost: float = 0.0
    error: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return (self.end if self.end is not None else self.start) - self.start


class Tracer:
    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self.clock = clock
        self.spans: List[Span] = []
        self._stack: List[Span] = []

    @contextmanager
    def span(self, name: str, cost: float = 0.0, **attrs: Any) -> Iterator[Span]:
        parent = self._stack[-1].id if self._stack else None
        span = Span(len(self.spans) + 1, parent, name, self.clock(), cost=cost, attrs=dict(attrs))
        self.spans.append(span)
        self._stack.append(span)
        try:
            yield span
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            span.end = self.clock()
            self._stack.pop()

    def children(self, span_id: Optional[int]) -> List[Span]:
        return [s for s in self.spans if s.parent == span_id]

    def total_cost(self, span_id: Optional[int] = None) -> float:
        """Cost of a span's whole subtree, or of the entire trace when ``span_id`` is None."""
        if span_id is None:
            return sum(s.cost for s in self.spans)
        span = next(s for s in self.spans if s.id == span_id)
        return span.cost + sum(self.total_cost(c.id) for c in self.children(span_id))

    def critical_path(self) -> List[Span]:
        """Follow the longest-running child at each level, from the longest root."""
        roots = self.children(None)
        if not roots:
            return []
        node = max(roots, key=lambda s: s.duration)
        path = [node]
        while True:
            kids = self.children(node.id)
            if not kids:
                return path
            node = max(kids, key=lambda s: s.duration)
            path.append(node)

    def errors(self) -> List[Span]:
        return [s for s in self.spans if s.error]

    def to_dicts(self) -> List[Dict[str, Any]]:
        return [{"id": s.id, "parent": s.parent, "name": s.name, "start": s.start, "duration": s.duration,
                 "cost": s.cost, "error": s.error, "attrs": s.attrs} for s in self.spans]

    def render(self) -> str:
        lines: List[str] = []

        def walk(span_id: Optional[int], depth: int) -> None:
            for span in self.children(span_id):
                flag = f"  ERROR {span.error}" if span.error else ""
                cost = f"  ${span.cost:.4f}" if span.cost else ""
                lines.append(f"{'  ' * depth}{span.name}  {span.duration * 1000:.1f}ms{cost}{flag}")
                walk(span.id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)
