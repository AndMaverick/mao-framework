"""Command line.

    python -m mao demo
    python -m mao diagnose answers.json
    python -m mao verify audit.jsonl [--head HASH] [--length N]
    python -m mao export audit.jsonl usage.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from . import Action, AuditLog, ModelRegistry, ModelSpec, Orchestrator, Policy, Step, report, write_usage_csv


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
    print(f"audit entries={len(audit.entries)} chain_valid={audit.verify()} head={audit.head[:16]}")
    return 0 if result.ok else 1


def diagnose(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        print(report(json.load(handle)))
    return 0


def verify(path: str, head: Optional[str], length: Optional[int]) -> int:
    with open(path, encoding="utf-8") as handle:
        log = AuditLog.from_jsonl(handle.read())
    result = log.check(head=head, length=length)
    if result.ok:
        anchored = " against the anchor" if head or length is not None else " (no anchor given, so truncation and full rewrites are not detectable)"
        print(f"valid: {len(log.entries)} entries, head {log.head}{anchored}")
        return 0
    where = f" at entry {result.index}" if result.index is not None else ""
    print(f"INVALID: {result.reason}{where}", file=sys.stderr)
    return 1


def export(path: str, out: str) -> int:
    with open(path, encoding="utf-8") as handle:
        log = AuditLog.from_jsonl(handle.read())
    if not log.verify():
        print("refusing to export an audit log that does not verify", file=sys.stderr)
        return 1
    print(f"{out}: {write_usage_csv(log, out)} metered attempt(s)")
    return 0


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(prog="mao", description="Model, Action, Orchestration.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    d = sub.add_parser("diagnose")
    d.add_argument("answers")
    v = sub.add_parser("verify")
    v.add_argument("log")
    v.add_argument("--head")
    v.add_argument("--length", type=int)
    e = sub.add_parser("export")
    e.add_argument("log")
    e.add_argument("out")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "demo":
            return demo()
        if args.cmd == "diagnose":
            return diagnose(args.answers)
        if args.cmd == "verify":
            return verify(args.log, args.head, args.length)
        return export(args.log, args.out)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
