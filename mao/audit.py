"""Tamper-evident audit trail. Each entry commits to the one before it."""

from __future__ import annotations

import hashlib
import json
import time

GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self.entries: list[dict] = []

    def append(self, event: str, **fields) -> dict:
        prev = self.entries[-1]["hash"] if self.entries else GENESIS
        body = {"seq": len(self.entries), "ts": self._clock(), "event": event, **fields}
        entry = {**body, "prev": prev, "hash": _digest(prev, body)}
        self.entries.append(entry)
        return entry

    def verify(self) -> bool:
        prev = GENESIS
        for index, entry in enumerate(self.entries):
            body = {k: v for k, v in entry.items() if k not in ("prev", "hash")}
            if entry["seq"] != index or entry["prev"] != prev or entry["hash"] != _digest(prev, body):
                return False
            prev = entry["hash"]
        return True

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(e, sort_keys=True, default=str) for e in self.entries)
