"""Pack the best, least redundant context into a fixed token budget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Sequence


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    score: float
    source: str = ""


def estimate_tokens(text: str) -> int:
    """About 1.33 tokens per word. Pass a real tokenizer as ``count`` when you have one."""
    return max(1, round(len(text.split()) * 1.33))


def overlap(a: str, b: str) -> float:
    """Jaccard similarity of word sets, case-insensitive."""
    left, right = set(a.lower().split()), set(b.lower().split())
    return len(left & right) / len(left | right) if left and right else 0.0


@dataclass
class Packed:
    chunks: List[Chunk]
    used: int
    budget: int
    dropped: List[Chunk]

    def text(self, separator: str = "\n\n") -> str:
        return separator.join(c.text for c in self.chunks)


def pack(chunks: Sequence[Chunk], budget: int, diversity: float = 0.0, pinned: Iterable[str] = (),
         count: Callable[[str], int] = estimate_tokens) -> Packed:
    """Greedy selection by score, with each candidate penalised by its redundancy against what is already chosen.

    ``pinned`` chunk ids are always included first. If they alone exceed the budget that is a caller error.
    The result never exceeds ``budget`` and is deterministic: ties break on score, then id.
    """
    by_id = {c.id: c for c in chunks}
    pinned_ids = list(pinned)
    unknown = [i for i in pinned_ids if i not in by_id]
    if unknown:
        raise ValueError(f"pinned chunk(s) not found: {', '.join(unknown)}")
    selected = [by_id[i] for i in pinned_ids]
    used = sum(count(c.text) for c in selected)
    if used > budget:
        raise ValueError(f"pinned chunks need {used} tokens, budget is {budget}")
    remaining = [c for c in chunks if c.id not in set(pinned_ids)]
    while remaining:
        def adjusted(chunk: Chunk) -> float:
            return chunk.score - diversity * max((overlap(chunk.text, s.text) for s in selected), default=0.0)

        best = max(remaining, key=lambda c: (adjusted(c), c.score, c.id))
        remaining.remove(best)
        cost = count(best.text)
        if used + cost <= budget:
            selected.append(best)
            used += cost
    chosen = {c.id for c in selected}
    return Packed(selected, used, budget, [c for c in chunks if c.id not in chosen])
