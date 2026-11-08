"""Command line: `python -m mao demo` or `python -m mao diagnose answers.json`."""

from __future__ import annotations

import json
import sys

from . import (Action, AuditLog, ModelRegistry, ModelSpec, Orchestrator, Policy, Step, report)


def demo() -> int:
    registry = ModelRegistry([
        ModelSpec("small", frozenset({"extract"}), 0.2, 120, 1),
        ModelSpec("large", frozenset({"extract", "draft"}), 3.0, 900, 2),
        ModelSpec("frontier", frozenset({"extract", "draft", "reason"}), 15.0, 2500, 3),
    ])
    policy = Policy("analyst", frozenset({"read", "write"}), max_risk=2, approval_risk=2)
    steps = [
        Step("retrieve", Action("retrieve", "read", 1, False, lambda c: ["q3.csv", "q4.csv"]),
             needs=frozenset({"extract"})),
        Step("draft", Action("draft", "read", 1, False, lambda c: f"summary of {len(c['deps']['retrieve'])} files"),
             needs=frozenset({"draft"}), depends_on=("retrieve",), verify=lambda out: bool(out)),
        Step("publish", Action("publish", "write", 2, True, lambda c: "published"),
             needs=frozenset({"draft"}), depends_on=("draft",)),
    ]
    audit = AuditLog()
    result = Orchestrator(registry, policy, audit, approver=lambda step: True, budget=10.0).run(steps)
    print(f"ok={result.ok} spend={result.spend} outputs={result.outputs}")
    print(f"audit entries={len(audit.entries)} chain_valid={audit.verify()}")
    return 0 if result.ok else 1


def diagnose(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        print(report(json.load(handle)))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["demo"]:
        return demo()
    if len(argv) == 2 and argv[0] == "diagnose":
        return diagnose(argv[1])
    print("usage: python -m mao demo | python -m mao diagnose answers.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
