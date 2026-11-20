"""The hand off contract from "Closed Values: Hand Off Contracts for Untrusted Agents".

A quarantined agent reads untrusted text and returns a short string. This module turns
that string into a Verdict made only of closed values, or refuses it. A second function
maps a Verdict to effects drawn from a fixed list.

This is the small version, written to be read in one sitting. The hardened version, with
signed messages, replay protection and an audit trail, is mao/handoff.py in this repository.
Python 3.9 or later, standard library only.
"""

import json
from typing import Any, NamedTuple, Tuple

LABELS = frozenset({"bug", "feature", "question", "docs", "spam"})
SEVERITIES = frozenset({"low", "medium", "high"})
MAX_LINES = 10
MAX_BYTES = 2048


class Rejected(Exception):
    """The quarantined output was refused. The message is a short fixed reason, never the output."""


class Verdict(NamedTuple):
    issue_id: int
    label: str
    severity: str
    lines: Tuple[int, ...]
    provenance: str


def _no_duplicate_keys(pairs: Any) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            raise Rejected("duplicate key")
        seen[key] = value
    return seen


def _reject_constant(name: str) -> Any:
    raise Rejected("constant")


def parse(raw, issue_id, file_lines):
    """Turn the quarantined model's raw output into a Verdict or raise Rejected."""
    if type(issue_id) is not int or type(file_lines) is not int:
        raise TypeError("issue_id and file_lines come from the caller and must be int")
    if type(raw) is not str or len(raw.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        raise Rejected("size")
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except Rejected:
        raise
    except (ValueError, RecursionError):
        raise Rejected("not json") from None
    if type(data) is not dict or set(data) != {"label", "severity", "lines"}:
        raise Rejected("keys")
    label, severity, lines = data["label"], data["severity"], data["lines"]
    if type(label) is not str or label not in LABELS:
        raise Rejected("label")
    if type(severity) is not str or severity not in SEVERITIES:
        raise Rejected("severity")
    if type(lines) is not list or len(lines) > MAX_LINES:
        raise Rejected("lines")
    for number in lines:
        if type(number) is not int or not 1 <= number <= file_lines:
            raise Rejected("line number")
    if lines != sorted(set(lines)):
        raise Rejected("line order")
    return Verdict(issue_id, label, severity, tuple(lines), f"untrusted:issue:{issue_id}")


ALLOWED_EFFECTS = frozenset(
    {f"add_label:{label}" for label in LABELS}
    | {"post_template:needs_repro", "request_human_review"}
)


def plan(verdict):
    """Map a Verdict to effects. Every effect comes from ALLOWED_EFFECTS, so the
    plan is harmless for every value the contract admits."""
    effects = [f"add_label:{verdict.label}"]
    if verdict.label == "bug":
        effects.append("post_template:needs_repro")
    if verdict.label == "spam" or verdict.severity == "high":
        effects.append("request_human_review")  # a person decides, never an auto close
    return tuple(effects)
