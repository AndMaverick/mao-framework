"""Tamper-evident audit trail. Each entry commits to the one before it.

A hash chain proves that no entry was changed, removed from the middle, or reordered. It cannot, on
its own, prove that nothing was cut from the end or that the whole log was not regenerated from
scratch. Both are closed by an anchor: record ``audit.anchor()`` (length and head hash) somewhere
the log's writer cannot edit, then verify against it with ``verify(head=..., length=...)``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

GENESIS = "0" * 64
RESERVED = frozenset({"seq", "ts", "event", "prev", "hash"})


def _digest(prev: str, body: Dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Verification:
    """Outcome of checking a log. ``index`` is the first entry that failed, when one did."""

    ok: bool
    reason: str = "ok"
    index: Optional[int] = None

    def __bool__(self) -> bool:
        return self.ok


class AuditLog:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self.entries: List[Dict[str, Any]] = []

    def append(self, event: str, **fields: Any) -> Dict[str, Any]:
        clash = RESERVED & fields.keys()
        if clash:
            raise ValueError(f"audit field name(s) reserved: {', '.join(sorted(clash))}")
        prev = self.head
        body = {"seq": len(self.entries), "ts": self._clock(), "event": event, **fields}
        entry = {**body, "prev": prev, "hash": _digest(prev, body)}
        self.entries.append(entry)
        return entry

    @property
    def head(self) -> str:
        return self.entries[-1]["hash"] if self.entries else GENESIS

    def anchor(self) -> Dict[str, Any]:
        """The two values to store outside the log's reach. Together they pin its exact content."""
        return {"length": len(self.entries), "head": self.head}

    def check(self, head: Optional[str] = None, length: Optional[int] = None) -> Verification:
        prev = GENESIS
        for index, entry in enumerate(self.entries):
            try:
                body = {k: v for k, v in entry.items() if k not in ("prev", "hash")}
                if entry["seq"] != index:
                    return Verification(False, "sequence number out of order", index)
                if entry["prev"] != prev:
                    return Verification(False, "entry does not follow the one before it", index)
                if entry["hash"] != _digest(prev, body):
                    return Verification(False, "entry content does not match its hash", index)
            except (KeyError, TypeError):
                return Verification(False, "entry is malformed", index)
            prev = entry["hash"]
        if length is not None and len(self.entries) != length:
            return Verification(False, f"log has {len(self.entries)} entries, anchor says {length}")
        if head is not None and prev != head:
            return Verification(False, "head hash does not match the anchor")
        return Verification(True)

    def verify(self, head: Optional[str] = None, length: Optional[int] = None) -> bool:
        return self.check(head, length).ok

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e, sort_keys=True, default=str) for e in self.entries)

    @classmethod
    def from_jsonl(cls, text: str) -> AuditLog:
        log = cls()
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {number} is not valid JSON: {exc.msg}") from None
            if not isinstance(entry, dict):
                raise ValueError(f"line {number} is not an audit entry")
            log.entries.append(entry)
        return log
