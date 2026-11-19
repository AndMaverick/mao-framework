"""Assurance: evaluate a system, detect when its inputs drift, and run an incident register.

Three things a governed system needs before anyone will trust it in production, with no dependencies.
Evaluation gates a release on measured regressions, drift watches the population the system now sees
against the one it was tested on, and the register records what went wrong, who must fix it by when and
how long fixes take.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Case:
    id: str
    input: Any
    expected: Any = None


Scorer = Callable[[Case, Any], float]


def exact(case: Case, output: Any) -> float:
    return 1.0 if str(output).strip() == str(case.expected).strip() else 0.0


def contains(case: Case, output: Any) -> float:
    return 1.0 if str(case.expected).lower() in str(output).lower() else 0.0


def regex(pattern: str) -> Scorer:
    compiled = re.compile(pattern)
    return lambda case, output: 1.0 if compiled.search(str(output)) else 0.0


def numeric_close(tolerance: float = 1e-6) -> Scorer:
    def scorer(case: Case, output: Any) -> float:
        try:
            return 1.0 if abs(float(output) - float(case.expected)) <= tolerance else 0.0
        except (TypeError, ValueError):
            return 0.0

    return scorer


@dataclass
class Suite:
    name: str
    cases: List[Case]
    scorers: Dict[str, Scorer] = field(default_factory=lambda: {"exact": exact})

    def __post_init__(self) -> None:
        ids = [c.id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("case ids must be unique")


@dataclass
class Report:
    suite: str
    scores: Dict[str, Dict[str, float]]
    errors: Dict[str, str]

    def mean(self, scorer: str) -> float:
        values = list(self.scores[scorer].values())
        return mean(values) if values else 0.0

    def pass_rate(self, scorer: str, threshold: float = 1.0) -> float:
        values = list(self.scores[scorer].values())
        return sum(1 for v in values if v >= threshold) / len(values) if values else 0.0


def run(suite: Suite, system: Callable[[Any], Any]) -> Report:
    """Run ``system(case.input)`` over every case. A crash scores zero and is recorded, never skipped."""
    scores: Dict[str, Dict[str, float]] = {name: {} for name in suite.scorers}
    errors: Dict[str, str] = {}
    for case in suite.cases:
        try:
            output = system(case.input)
        except Exception as exc:
            errors[case.id] = f"{type(exc).__name__}: {exc}"
            for name in suite.scorers:
                scores[name][case.id] = 0.0
            continue
        for name, scorer in suite.scorers.items():
            value = float(scorer(case, output))
            if not 0.0 <= value <= 1.0 or math.isnan(value):
                raise ValueError(f"scorer '{name}' returned {value} for case {case.id}; scores must be in [0, 1]")
            scores[name][case.id] = value
    return Report(suite.name, scores, errors)


@dataclass
class Gate:
    ok: bool
    regressions: List[Tuple[str, Any, Any]]  # (scorer, drop or "missing", candidate mean)
    newly_failing: List[str]  # cases that passed in the baseline and fail in the candidate


def gate(baseline: Report, candidate: Report, max_drop: float = 0.02, threshold: float = 1.0) -> Gate:
    """Fail when a scorer's mean falls more than ``max_drop``, or a scorer vanishes.

    ``newly_failing`` lists individual cases that regressed even when the mean held, because an average
    can hide one important case going from pass to fail while others improve. It is reported and does not
    fail the gate on its own, so the caller decides how strict to be.
    """
    regressions: List[Tuple[str, Any, Any]] = []
    newly: List[str] = []
    for scorer in baseline.scores:
        if scorer not in candidate.scores:
            regressions.append((scorer, "missing", None))
            continue
        drop = baseline.mean(scorer) - candidate.mean(scorer)
        if drop > max_drop:
            regressions.append((scorer, round(drop, 4), candidate.mean(scorer)))
        for case_id, before in baseline.scores[scorer].items():
            after = candidate.scores[scorer].get(case_id)
            if before >= threshold and (after is None or after < threshold) and case_id not in newly:
                newly.append(case_id)
    return Gate(not regressions, regressions, sorted(newly))


# --------------------------------------------------------------------- drift

_EPS = 1e-4


def _edges(values: Sequence[float], bins: int) -> List[float]:
    ordered = sorted(values)
    return sorted({ordered[min(len(ordered) - 1, int(i * len(ordered) / bins))] for i in range(1, bins)})


def _shares(values: Sequence[float], edges: Sequence[float]) -> List[float]:
    counts = [0] * (len(edges) + 1)
    for value in values:
        counts[bisect_right(edges, value)] += 1
    return [max(c / len(values), _EPS) for c in counts]


def _psi(expected: Sequence[float], actual: Sequence[float]) -> float:
    return sum((a - e) * math.log(a / e) for e, a in zip(expected, actual))


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float:
    """Population Stability Index of ``actual`` against ``expected``, on the expected sample's quantile bins."""
    if not expected or not actual:
        raise ValueError("both samples must be non-empty")
    if bins < 2:
        raise ValueError("bins must be at least 2")
    edges = _edges(expected, bins)
    return _psi(_shares(expected, edges), _shares(actual, edges))


def categorical_psi(expected: Mapping[str, float], actual: Mapping[str, float]) -> float:
    e_total, a_total = sum(expected.values()), sum(actual.values())
    if e_total <= 0 or a_total <= 0:
        raise ValueError("both distributions must have positive total")
    keys = sorted(set(expected) | set(actual))
    return _psi([max(expected.get(k, 0) / e_total, _EPS) for k in keys], [max(actual.get(k, 0) / a_total, _EPS) for k in keys])


def ks_stat(a: Sequence[float], b: Sequence[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic: the largest gap between the empirical distributions."""
    if not a or not b:
        raise ValueError("both samples must be non-empty")
    left, right = sorted(a), sorted(b)
    return max(abs(bisect_right(left, p) / len(left) - bisect_right(right, p) / len(right)) for p in sorted(set(left) | set(right)))


def drift_status(value: float) -> str:
    """Conventional PSI reading: under 0.1 stable, under 0.25 moderate, otherwise major."""
    return "stable" if value < 0.1 else "moderate" if value < 0.25 else "major"


def drift_report(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> Dict[str, Any]:
    p = psi(expected, actual, bins)
    return {"psi": round(p, 4), "ks": round(ks_stat(expected, actual), 4), "status": drift_status(p)}


# ----------------------------------------------------------------- incidents

SLA_HOURS = {"SEV1": 4, "SEV2": 24, "SEV3": 72, "SEV4": 168}


def risk_score(impact: int, likelihood: int, detectability: int) -> int:
    """Each factor is 1 to 5. High detectability lowers risk: 5 means the failure is caught fast."""
    for name, value in (("impact", impact), ("likelihood", likelihood), ("detectability", detectability)):
        if not 1 <= value <= 5:
            raise ValueError(f"{name} must be 1 to 5")
    return impact * likelihood * (6 - detectability)


def severity(score: int) -> str:
    return "SEV1" if score >= 60 else "SEV2" if score >= 30 else "SEV3" if score >= 12 else "SEV4"


@dataclass
class Incident:
    id: str
    title: str
    score: int
    severity: str
    opened: datetime
    resolved: Optional[datetime] = None
    cause: str = ""
    fix: str = ""

    @property
    def deadline(self) -> datetime:
        return self.opened + timedelta(hours=SLA_HOURS[self.severity])


class Register:
    def __init__(self) -> None:
        self.incidents: Dict[str, Incident] = {}

    def open(self, id: str, title: str, impact: int, likelihood: int, detectability: int, when: datetime) -> Incident:
        if id in self.incidents:
            raise ValueError(f"incident exists: {id}")
        score = risk_score(impact, likelihood, detectability)
        incident = Incident(id, title, score, severity(score), when)
        self.incidents[id] = incident
        return incident

    def resolve(self, id: str, when: datetime, cause: str = "", fix: str = "") -> Incident:
        incident = self.incidents[id]
        if when < incident.opened:
            raise ValueError("an incident cannot be resolved before it was opened")
        incident.resolved, incident.cause, incident.fix = when, cause, fix
        return incident

    def breached(self, now: datetime) -> List[Incident]:
        """Unresolved incidents past their SLA deadline, most severe first, then oldest."""
        late = [i for i in self.incidents.values() if i.resolved is None and now > i.deadline]
        return sorted(late, key=lambda i: (i.severity, i.opened))

    def mttr_hours(self) -> Optional[float]:
        done = [i for i in self.incidents.values() if i.resolved is not None]
        if not done:
            return None
        return sum((i.resolved - i.opened).total_seconds() for i in done if i.resolved) / len(done) / 3600

    def postmortem(self, id: str) -> str:
        i = self.incidents[id]
        status = f"resolved {i.resolved:%Y-%m-%d %H:%M}" if i.resolved else "open"
        return "\n".join([
            f"# Postmortem: {i.title}", "",
            f"- ID: {i.id}", f"- Severity: {i.severity} (risk score {i.score})",
            f"- Opened: {i.opened:%Y-%m-%d %H:%M}", f"- Status: {status}", "",
            "## Cause", i.cause or "_to be completed_", "",
            "## Fix", i.fix or "_to be completed_", "",
            "## Follow-ups", "- [ ] Add a detection for this failure mode", "- [ ] Add a regression case to the eval suite",
        ])
