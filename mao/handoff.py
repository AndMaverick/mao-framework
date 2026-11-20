"""Handoff contract between a quarantined reader and a privileged agent.

The problem. One agent reads text an outsider wrote (an issue, an email, a ticket). A second agent holds the
tools and the data. If the first agent's output can carry text, an instruction hidden in the outside text can ride
through a schema-valid field and reach the second agent. A free text field is an injection channel with a type
annotation.

The contract. The reader may return exactly three things, each drawn from a closed set the privileged side owns:

* a label from a fixed list,
* a severity from a fixed list,
* line numbers, as integers inside a bound that comes from the item itself.

Everything else is attached by trusted code, never by the reader: the item reference, the provenance, a nonce and a
tag. The privileged side then does four things on its own. It checks the tag. It looks the item up itself. It derives
the effect from its own routing table, so the message carries no effect to forge. And it asks the Action layer
whether that effect is allowed, so an approval gate or a denial still applies.

What this does and does not give you is written out in docs/HANDOFF.md. The short version: it bounds what kind of
thing the reader can cause. It does not make the reader's choice correct, and a reader steered into a wrong label
is a wrong label, not a breach. Design every effect so that it is harmless for every value in the table.

Standard library only. Python 3.9 or newer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Tuple

from .action import Action, Policy
from .audit import AuditLog

VERSION = 1
MAX_READER_BYTES = 2048
MAX_MESSAGE_BYTES = 4096
MAX_LINE_BOUND = 10_000_000

_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")

REASONS = frozenset(
    {
        "ok", "not_text", "too_large", "not_utf8", "not_json", "duplicate_key", "bad_number", "not_object", "bad_keys",
        "bad_label", "bad_severity", "bad_lines", "line_out_of_range", "bad_ref", "bad_provenance", "bad_version",
        "bad_nonce", "bad_tag", "wrong_schema", "replay", "unknown_ref", "no_route", "approval_required", "denied_by_policy",
        "escalation_budget", "executor_failed", "bad_message",
    }
)


class HandoffError(ValueError):
    """A message was refused. ``code`` is one of ``REASONS`` and is the only thing that is ever logged about the refusal,
    so a refused message cannot smuggle its own text into a log or a review queue."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Schema:
    """The closed vocabulary. ``severities`` runs from least to most serious."""

    labels: Tuple[str, ...]
    severities: Tuple[str, ...]
    provenances: Tuple[str, ...] = ("reader",)
    escalate_labels: FrozenSet[str] = frozenset()
    escalate_at: Optional[str] = None
    max_lines: int = 20

    def __post_init__(self) -> None:
        for what, values in (("labels", self.labels), ("severities", self.severities), ("provenances", self.provenances)):
            if not values or len(set(values)) != len(values):
                raise ValueError(f"{what}: a non-empty list without duplicates")
            if not all(type(v) is str and _NAME.fullmatch(v) for v in values):
                raise ValueError(f"{what}: names are lower case ASCII words")
        if not set(self.escalate_labels) <= set(self.labels):
            raise ValueError("escalate_labels must be labels")
        if self.escalate_at is not None and self.escalate_at not in self.severities:
            raise ValueError("escalate_at must be a severity")
        if not 0 <= self.max_lines <= 200:
            raise ValueError("max_lines: 0 to 200")

    def fingerprint(self) -> str:
        body = {
            "labels": list(self.labels), "severities": list(self.severities), "provenances": list(self.provenances),
            "max_lines": self.max_lines, "v": VERSION,
        }
        return hashlib.sha256(_canonical(body)).hexdigest()

    def escalates(self, label: str, severity: str) -> bool:
        if label in self.escalate_labels:
            return True
        return self.escalate_at is not None and self.severities.index(severity) >= self.severities.index(self.escalate_at)


TRIAGE = Schema(
    labels=("bug", "feature", "question", "docs", "spam"),
    severities=("low", "medium", "high", "critical"),
    escalate_labels=frozenset({"spam"}),
    escalate_at="high",
)

# The effects a handoff can ever cause. Each is an ordinary Action, so the Policy that already governs the rest of the
# system decides on it. Posting needs a human by default because it risks at the approval threshold.
EFFECTS: Dict[str, Action] = {
    "apply_label": Action("apply_label", "issues:label", 1, True),
    "post_ack": Action("post_ack", "issues:comment", 2, True),
    "escalate": Action("escalate", "review:queue", 1, True),
}


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


@dataclass(frozen=True)
class Verdict:
    """What the reader produced, after validation. Closed values only."""

    label: str
    severity: str
    lines: Tuple[int, ...] = ()


def _strict_int(x: Any) -> bool:
    return type(x) is int


def _check_verdict_fields(obj: Any, schema: Schema, max_line: int) -> Verdict:
    if type(obj) is not dict:
        raise HandoffError("not_object")
    if set(obj) != {"label", "severity", "lines"}:
        raise HandoffError("bad_keys")
    label, severity, lines = obj["label"], obj["severity"], obj["lines"]
    if type(label) is not str or label not in schema.labels:
        raise HandoffError("bad_label")
    if type(severity) is not str or severity not in schema.severities:
        raise HandoffError("bad_severity")
    if type(lines) is not list or len(lines) > schema.max_lines:
        raise HandoffError("bad_lines")
    previous = 0
    for n in lines:
        if not _strict_int(n):
            raise HandoffError("bad_lines")
        if n < 1 or n > max_line:
            raise HandoffError("line_out_of_range")
        if n <= previous:                      # one canonical form: strictly increasing, no duplicates
            raise HandoffError("bad_lines")
        previous = n
    return Verdict(label, severity, tuple(lines))


def _bound(max_line: Any) -> int:
    if not _strict_int(max_line) or not 0 <= max_line <= MAX_LINE_BOUND:
        raise ValueError("max_line: a whole number from 0 to 10,000,000, taken from the item itself")
    return int(max_line)


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise HandoffError("duplicate_key")
        out[k] = v
    return out


def _refuse_number(_: str) -> Any:
    raise HandoffError("bad_number")


def parse_reader_output(raw: Any, schema: Schema, *, max_line: int) -> Verdict:
    """Turn the reader's raw reply into a Verdict, or raise HandoffError with a fixed code.

    ``max_line`` must come from the item the reader was shown (its real line count), never from the reader."""
    bound = _bound(max_line)
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    elif type(raw) is str:
        data = raw.encode("utf-8", "surrogatepass")
    else:
        raise HandoffError("not_text")
    if len(data) > MAX_READER_BYTES:
        raise HandoffError("too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HandoffError("not_utf8") from None
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_refuse_number, parse_constant=_refuse_number)
    except HandoffError:
        raise
    except (ValueError, RecursionError):
        raise HandoffError("not_json") from None
    return _check_verdict_fields(obj, schema, bound)


def domain(schema: Schema, max_line: int) -> Iterator[Verdict]:
    """Every value the reader can legitimately produce: all label and severity pairs, with the line shapes that matter
    (none, the first line, the last line, the first and last, and the longest run allowed). The set is finite, so a
    property about every admitted value can be checked by running all of them."""
    bound = _bound(max_line)
    shapes: List[Tuple[int, ...]] = [()]
    if bound >= 1:
        shapes += [(1,), (bound,)]
        if bound >= 2:
            shapes.append((1, bound))
        run = tuple(range(1, min(bound, schema.max_lines) + 1))
        if run and run not in shapes:
            shapes.append(run)
    for label in schema.labels:
        for severity in schema.severities:
            for shape in shapes:
                yield Verdict(label, severity, shape)


class Sealer:
    """Trusted side of the reader. Holds the key, validates the reader's output, attaches reference, provenance, nonce and tag.

    The reader process must never hold the key or be able to call ``seal``. If it can, the tag proves nothing."""

    def __init__(self, key: bytes, schema: Schema = TRIAGE, provenance: str = "reader",
                 nonce: Callable[[], str] = lambda: secrets.token_hex(16)) -> None:
        if type(key) is not bytes or len(key) < 16:
            raise ValueError("key: at least 16 bytes of random data")
        if provenance not in schema.provenances:
            raise ValueError("provenance must be one of the schema's provenances")
        self._key, self.schema, self.provenance, self._nonce = key, schema, provenance, nonce

    def seal(self, verdict: Verdict, *, ref: str, max_line: int) -> Dict[str, Any]:
        if type(ref) is not str or not _REF.fullmatch(ref):
            raise HandoffError("bad_ref")
        checked = _check_verdict_fields(
            {"label": verdict.label, "severity": verdict.severity, "lines": list(verdict.lines)}, self.schema, _bound(max_line)
        )
        body = {
            "v": VERSION, "schema": self.schema.fingerprint(), "ref": ref, "provenance": self.provenance,
            "label": checked.label, "severity": checked.severity, "lines": list(checked.lines), "nonce": self._nonce(),
        }
        body["tag"] = hmac.new(self._key, _canonical(body), hashlib.sha256).hexdigest()
        return body

    def seal_reader_output(self, raw: Any, *, ref: str, max_line: int) -> Dict[str, Any]:
        return self.seal(parse_reader_output(raw, self.schema, max_line=max_line), ref=ref, max_line=max_line)


@dataclass(frozen=True)
class Item:
    """What the privileged side knows about the thing the reader looked at, from its own store."""

    ref: str
    line_count: int


@dataclass(frozen=True)
class Context:
    """The only arguments an executor ever receives. Every field is a closed value or a bounded integer."""

    ref: str
    label: str
    severity: str
    lines: Tuple[int, ...]


@dataclass(frozen=True)
class Outcome:
    status: str                   # done, pending_approval, denied, rejected, deferred, failed
    reason: str                   # one of REASONS
    ref: Optional[str] = None
    effects: Tuple[str, ...] = ()


class ReplayGuard:
    """Remembers recent nonces. A bounded memory: a replay older than ``capacity`` messages is not caught here, so keep
    effects idempotent per reference as well."""

    def __init__(self, capacity: int = 100_000) -> None:
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def first_use(self, nonce: str) -> bool:
        if nonce in self._seen:
            return False
        self._seen[nonce] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


class EscalationBudget:
    """A rate limit on human escalations, per provenance. An attacker who can influence the reader can make it say "spam"
    or "critical" on demand; without a limit that floods the reviewers. Over the limit, escalations are deferred and
    counted, not dropped silently."""

    def __init__(self, limit: int = 20, per_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.per_seconds, self._clock = limit, per_seconds, clock
        self._times: Dict[str, List[float]] = {}
        self.deferred = 0

    def allow(self, provenance: str) -> bool:
        now = self._clock()
        recent = [t for t in self._times.get(provenance, []) if now - t < self.per_seconds]
        if len(recent) >= self.limit:
            self._times[provenance] = recent
            self.deferred += 1
            return False
        recent.append(now)
        self._times[provenance] = recent
        return True


Executor = Callable[[Context], Any]
Approver = Callable[[str, Context], bool]


def default_routes(schema: Schema) -> Dict[Tuple[str, str], Tuple[str, ...]]:
    """A routing table that covers the whole domain. Escalation cases go to a human and nowhere else. Questions get the
    canned acknowledgement. Everything else is labelled. The table belongs to the privileged side; the message has no
    effect field."""
    routes: Dict[Tuple[str, str], Tuple[str, ...]] = {}
    for label in schema.labels:
        for severity in schema.severities:
            if schema.escalates(label, severity):
                routes[(label, severity)] = ("escalate",) if label in schema.escalate_labels else ("apply_label", "escalate")
            elif label == "question":
                routes[(label, severity)] = ("apply_label", "post_ack")
            else:
                routes[(label, severity)] = ("apply_label",)
    return routes


@dataclass
class Gate:
    """Privileged side. Verifies, looks up, routes, asks the policy, executes, and logs only closed values."""

    key: bytes
    resolve: Callable[[str], Optional[Item]]
    executors: Dict[str, Executor]
    policy: Policy
    schema: Schema = TRIAGE
    routes: Optional[Dict[Tuple[str, str], Tuple[str, ...]]] = None
    approver: Optional[Approver] = None
    audit: Optional[AuditLog] = None
    budget: EscalationBudget = field(default_factory=EscalationBudget)
    replay: ReplayGuard = field(default_factory=ReplayGuard)
    owner: str = ""                            # the person who answers for these effects; see Policy.require_owner

    def __post_init__(self) -> None:
        if type(self.key) is not bytes or len(self.key) < 16:
            raise ValueError("key: at least 16 bytes of random data")
        if self.routes is None:
            self.routes = default_routes(self.schema)
        wanted = {(label, sev) for label in self.schema.labels for sev in self.schema.severities}
        if set(self.routes) != wanted:
            raise ValueError("routes must cover every label and severity pair, and nothing else")
        for effects in self.routes.values():
            for name in effects:
                if name not in EFFECTS:
                    raise ValueError(f"route names an effect that is not in the catalogue: {name!r}")
                if name not in self.executors:
                    raise ValueError(f"no executor registered for {name!r}")

    # -- verification ---------------------------------------------------------------------------------------

    def _verify(self, message: Any) -> Tuple[Dict[str, Any], Context]:
        if type(message) is not dict:
            raise HandoffError("bad_message")
        try:
            size = len(json.dumps(message, sort_keys=True, ensure_ascii=True, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            raise HandoffError("bad_message") from None
        if size > MAX_MESSAGE_BYTES:
            raise HandoffError("too_large")
        if set(message) != {"v", "schema", "ref", "provenance", "label", "severity", "lines", "nonce", "tag"}:
            raise HandoffError("bad_keys")
        if not _strict_int(message["v"]) or message["v"] != VERSION:
            raise HandoffError("bad_version")
        ref, tag, nonce, prov = message["ref"], message["tag"], message["nonce"], message["provenance"]
        if type(ref) is not str or not _REF.fullmatch(ref):
            raise HandoffError("bad_ref")
        if type(prov) is not str or prov not in self.schema.provenances:
            raise HandoffError("bad_provenance")
        if type(nonce) is not str or not _HEX32.fullmatch(nonce):
            raise HandoffError("bad_nonce")
        if type(tag) is not str or not _HEX64.fullmatch(tag):
            raise HandoffError("bad_tag")
        if message["schema"] != self.schema.fingerprint():
            raise HandoffError("wrong_schema")
        body = {k: v for k, v in message.items() if k != "tag"}
        try:
            expected = hmac.new(self.key, _canonical(body), hashlib.sha256).hexdigest()
        except (TypeError, ValueError):
            raise HandoffError("bad_message") from None
        if not hmac.compare_digest(expected, tag):
            raise HandoffError("bad_tag")
        # The tag is good, so the fields are what the trusted sealer wrote. Validate them again anyway: this side does not
        # lean on the other side having done its job, and the line bound is re-derived from this side's own item.
        item = self.resolve(ref)
        if item is None or type(item) is not Item or item.ref != ref:
            raise HandoffError("unknown_ref")
        try:
            bound = _bound(item.line_count)
        except ValueError:
            raise HandoffError("unknown_ref") from None
        verdict = _check_verdict_fields(
            {"label": message["label"], "severity": message["severity"], "lines": message["lines"]}, self.schema, bound
        )
        if not self.replay.first_use(nonce):
            raise HandoffError("replay")
        return message, Context(ref, verdict.label, verdict.severity, verdict.lines)

    # -- handling -------------------------------------------------------------------------------------------

    def _log(self, outcome: Outcome, prov: str = "unknown") -> None:
        if self.audit is not None:
            self.audit.append("handoff", status=outcome.status, reason=outcome.reason, ref=outcome.ref or "none",
                              effects=list(outcome.effects), provenance=prov)

    def handle(self, message: Any) -> Outcome:
        try:
            message, ctx = self._verify(message)
        except HandoffError as e:
            outcome = Outcome("rejected", e.code)
            self._log(outcome)
            return outcome
        prov = message["provenance"]
        assert self.routes is not None
        planned = self.routes.get((ctx.label, ctx.severity))
        if planned is None:                    # unreachable while the constructor holds, kept as a closed door
            outcome = Outcome("rejected", "no_route", ctx.ref)
            self._log(outcome, prov)
            return outcome
        done: List[str] = []
        status, reason = "done", "ok"
        for name in planned:
            action = replace(EFFECTS[name], owner=self.owner)
            decision = self.policy.check(action)
            if not decision.allowed:
                status, reason = "denied", "denied_by_policy"
                continue
            if name == "escalate" and not self.budget.allow(prov):
                if status == "done":
                    status, reason = "deferred", "escalation_budget"
                continue
            if decision.needs_approval:
                approved = False
                if self.approver is not None:
                    try:
                        approved = self.approver(name, ctx) is True
                    except Exception:          # an approver that raises denies; it never waves anything through
                        approved = False
                if not approved:
                    if status in ("done", "deferred"):
                        status, reason = "pending_approval", "approval_required"
                    continue
            try:
                self.executors[name](ctx)
            except Exception:
                status, reason = "failed", "executor_failed"
                continue
            done.append(name)
        outcome = Outcome(status, reason, ctx.ref, tuple(done))
        self._log(outcome, prov)
        return outcome
