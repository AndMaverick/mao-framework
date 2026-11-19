"""Governance controls that run, pass or fail, with the evidence written down.

Each control maps a requirement area from the EU AI Act, the NIST AI RMF or GDPR to a piece of evidence a system
should be able to show. ``scan`` reports every control as met, missing or failed. ``runtime_evidence`` builds part of
that evidence from what MAO actually did (a verified audit log, an approval gate in the policy), so the claim is
checked, not asserted. Bias metrics are computed from outcomes with the standard library.

This is a readiness aid. It does not decide what a regulation requires and it is not legal advice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .action import Policy
from .audit import AuditLog

FRAMEWORKS = ("eu-ai-act", "nist-ai-rmf", "gdpr")

# control id -> (title, evidence key, {framework: reference})
CONTROLS: Dict[str, Tuple[str, str, Dict[str, str]]] = {
    "risk-classification": ("Risk classification recorded", "risk_class", {"eu-ai-act": "Art. 6 and Annex III", "nist-ai-rmf": "MAP"}),
    "risk-management": ("Risk management process documented", "risk_management", {"eu-ai-act": "Art. 9", "nist-ai-rmf": "MANAGE"}),
    "data-governance": ("Training data governance documented", "data_governance", {"eu-ai-act": "Art. 10", "nist-ai-rmf": "MAP"}),
    "bias-testing": ("Bias or fairness test with a result", "bias_score", {"eu-ai-act": "Art. 10", "nist-ai-rmf": "MEASURE"}),
    "record-keeping": ("Tamper-evident audit logging", "audit_logging", {"eu-ai-act": "Art. 12", "gdpr": "Art. 5(2)"}),
    "transparency": ("User-facing transparency notice", "transparency_notice", {"eu-ai-act": "Art. 13", "gdpr": "Arts. 13 to 15"}),
    "human-oversight": ("Human oversight mechanism", "human_oversight", {"eu-ai-act": "Art. 14", "nist-ai-rmf": "GOVERN", "gdpr": "Art. 22"}),
    "data-minimization": ("Data minimization reviewed", "data_minimization", {"gdpr": "Art. 5(1)(c)"}),
    "accountable-owner": ("Named accountable owner", "owner", {"nist-ai-rmf": "GOVERN", "gdpr": "Art. 5(2)"}),
}


@dataclass(frozen=True)
class Finding:
    control: str
    title: str
    reference: str
    status: str  # "met", "missing" or "failed"
    detail: str = ""


def controls_for(framework: str) -> List[str]:
    if framework not in FRAMEWORKS:
        raise ValueError(f"unknown framework: {framework}. Choose from {', '.join(FRAMEWORKS)}")
    return [cid for cid, (_, _, refs) in CONTROLS.items() if framework in refs]


def scan(metadata: Mapping[str, Any], framework: str, bias_threshold: float = 0.1) -> List[Finding]:
    """Evaluate every control for a framework. Met means the evidence key is present and truthy.

    The bias control also fails when the recorded gap exceeds ``bias_threshold``.
    """
    findings = []
    for cid in controls_for(framework):
        title, key, refs = CONTROLS[cid]
        value = metadata.get(key)
        if value is None or value == "" or value is False:
            findings.append(Finding(cid, title, refs[framework], "missing", f"no evidence for '{key}'"))
        elif cid == "bias-testing":
            try:
                gap = float(value)
            except (TypeError, ValueError):
                findings.append(Finding(cid, title, refs[framework], "failed", f"bias score {value!r} is not a number"))
                continue
            if gap > bias_threshold:
                findings.append(Finding(cid, title, refs[framework], "failed", f"bias gap {gap} exceeds threshold {bias_threshold}"))
            else:
                findings.append(Finding(cid, title, refs[framework], "met"))
        else:
            findings.append(Finding(cid, title, refs[framework], "met"))
    return findings


def readiness(findings: Sequence[Finding]) -> float:
    """Share of controls met, from 0 to 1."""
    return sum(f.status == "met" for f in findings) / len(findings) if findings else 0.0


def runtime_evidence(audit: Optional[AuditLog] = None, policy: Optional[Policy] = None) -> Dict[str, Any]:
    """Evidence MAO can verify itself.

    ``audit_logging`` is true only if the log is non-empty and its hash chain verifies. ``human_oversight`` is true
    only if the policy has an approval threshold within its risk ceiling and the log records at least one approval
    decision, so a gate that was configured but never exercised does not count.
    """
    evidence: Dict[str, Any] = {}
    if audit is not None:
        evidence["audit_logging"] = bool(audit.entries) and audit.verify()
        approvals = [e for e in audit.entries if e.get("event") == "approval"]
        gate_configured = policy is not None and policy.approval_risk <= policy.max_risk
        evidence["human_oversight"] = gate_configured and bool(approvals)
        evidence["approvals_recorded"] = len(approvals)
    return evidence


# --------------------------------------------------------------------------- bias

def rate_by_group(outcomes: Sequence[int], groups: Sequence[str]) -> Dict[str, float]:
    """Positive-outcome rate per group. ``outcomes`` are 0 or 1."""
    if len(outcomes) != len(groups):
        raise ValueError("outcomes and groups must be the same length")
    if not outcomes:
        raise ValueError("no outcomes")
    if any(o not in (0, 1) for o in outcomes):
        raise ValueError("outcomes must be 0 or 1")
    totals: Dict[str, List[int]] = {}
    for o, g in zip(outcomes, groups):
        totals.setdefault(g, []).append(o)
    return {g: sum(v) / len(v) for g, v in sorted(totals.items())}


def bias_report(outcomes: Sequence[int], groups: Sequence[str], min_group_size: int = 30) -> Dict[str, Any]:
    """Demographic parity gap, disparate impact ratio and the groups too small to trust.

    ``bias_score`` is the largest gap between any two groups' positive rates, the value ``scan`` reads. The ratio is
    the lowest rate over the highest; 0.8 is the conventional screening line (the four-fifths rule), a prompt to
    investigate and not a legal finding.
    """
    rates = rate_by_group(outcomes, groups)
    sizes: Dict[str, int] = {}
    for g in groups:
        sizes[g] = sizes.get(g, 0) + 1
    high, low = max(rates.values()), min(rates.values())
    return {
        "rates": {g: round(r, 4) for g, r in rates.items()},
        "bias_score": round(high - low, 4),
        "disparate_impact_ratio": None if high == 0 else round(low / high, 4),
        "below_four_fifths": high > 0 and low / high < 0.8,
        "small_groups": sorted(g for g, n in sizes.items() if n < min_group_size),
    }


# --------------------------------------------------------------------------- report

_MARK = {"met": "MET", "missing": "MISSING", "failed": "FAILED"}


def render(model: str, framework: str, findings: Sequence[Finding], bias: Optional[Mapping[str, Any]] = None) -> str:
    """A Markdown audit summary with a readiness score and an action checklist."""
    score = readiness(findings)
    lines = [f"# Audit summary: {model}", "", f"- Framework: {framework}",
             f"- Controls met: {sum(f.status == 'met' for f in findings)} of {len(findings)} ({score:.0%})",
             "- Note: readiness aid, not legal advice", "", "## Controls", "", "| Control | Reference | Status | Detail |", "|---|---|---|---|"]
    lines += [f"| {f.title} | {f.reference} | {_MARK[f.status]} | {f.detail or '-'} |" for f in findings]
    if bias:
        lines += ["", "## Bias metrics", ""] + [f"- {k}: {v}" for k, v in sorted(bias.items())]
    open_items = [f for f in findings if f.status != "met"]
    lines += ["", "## Actions", ""] + ([f"- [ ] {f.title}: {f.detail}" for f in open_items] or ["- No open gaps."])
    return "\n".join(lines) + "\n"
