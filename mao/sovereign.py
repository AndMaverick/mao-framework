"""Data residency: where does the data go when an AI system runs?

Model an architecture as nodes in jurisdictions and flows of named data classes, then check it against the
residency rules you define. Jurisdiction labels are yours (``AE``, ``EU``, ``on-prem``), so it fits any regime.
It enforces the rules you give it. It does not know what any law requires, and it is not legal advice.

Three things make it more than a lookup table:

* **Groups.** A rule can name a group (``GCC``) that expands to member jurisdictions.
* **Derived classes.** A prompt built from personal data is personal data. ``derives={"prompt": {"personal"}}``
  makes every flow of ``prompt`` obey the rules of ``personal`` as well.
* **Unclassified flows.** A data class with no rule is reported, because "unrestricted" by omission is how
  residency gaps start.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple


@dataclass(frozen=True)
class Node:
    name: str
    jurisdiction: str


@dataclass(frozen=True)
class Flow:
    src: str
    dst: str
    data_class: str


@dataclass(frozen=True)
class Violation:
    flow: Flow
    node: str
    jurisdiction: str
    rule_class: str
    reason: str


@dataclass(frozen=True)
class Residency:
    """Residency rules.

    rules:   data class -> jurisdictions (or group names) where it may be processed.
    groups:  group name -> member jurisdictions.
    derives: data class -> classes whose rules it also inherits.
    """

    rules: Mapping[str, Iterable[str]]
    groups: Mapping[str, Iterable[str]] = field(default_factory=dict)
    derives: Mapping[str, Iterable[str]] = field(default_factory=dict)

    def allowed(self, data_class: str) -> Optional[FrozenSet[str]]:
        if data_class not in self.rules:
            return None
        out: Set[str] = set()
        for item in self.rules[data_class]:
            out |= set(self.groups[item]) if item in self.groups else {item}
        return frozenset(out)

    def classes_for(self, data_class: str) -> List[str]:
        """The class itself plus everything it derives from, transitively, nearest first."""
        seen: List[str] = []
        queue = deque([data_class])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.append(current)
            queue.extend(self.derives.get(current, ()))
        return seen


def check(nodes: Mapping[str, Node], flows: Iterable[Flow], residency: Residency) -> List[Violation]:
    """Every flow end that breaks a rule. Both ends count: data is processed at the source as well as the destination."""
    violations: List[Violation] = []
    for flow in flows:
        unknown = [n for n in (flow.src, flow.dst) if n not in nodes]
        for name in unknown:
            violations.append(Violation(flow, name, "?", flow.data_class, f"unknown node '{name}'"))
        if unknown:
            continue
        for rule_class in residency.classes_for(flow.data_class):
            permitted = residency.allowed(rule_class)
            if permitted is None:
                continue
            for end in dict.fromkeys((flow.src, flow.dst)):
                where = nodes[end].jurisdiction
                if where not in permitted:
                    via = "" if rule_class == flow.data_class else f" (a {flow.data_class} carries {rule_class})"
                    violations.append(Violation(flow, end, where, rule_class, f"{rule_class} may not be processed in {where} at {end}{via}"))
    return violations


def unclassified(flows: Iterable[Flow], residency: Residency) -> List[str]:
    """Data classes that appear in flows but have no residency rule, directly or through derivation."""
    missing = {f.data_class for f in flows if all(residency.allowed(c) is None for c in residency.classes_for(f.data_class))}
    return sorted(missing)


def exposure(nodes: Mapping[str, Node], flows: Iterable[Flow]) -> Dict[str, List[str]]:
    """For each data class, the jurisdictions it touches."""
    touched: Dict[str, Set[str]] = defaultdict(set)
    for flow in flows:
        for end in (flow.src, flow.dst):
            if end in nodes:
                touched[flow.data_class].add(nodes[end].jurisdiction)
    return {k: sorted(v) for k, v in sorted(touched.items())}


def egress(nodes: Mapping[str, Node], flows: Iterable[Flow], home: str) -> List[Flow]:
    """Flows that leave the home jurisdiction."""
    return [f for f in flows if f.src in nodes and f.dst in nodes and nodes[f.src].jurisdiction == home and nodes[f.dst].jurisdiction != home]


def reach(flows: Iterable[Flow], start: str, data_class: Optional[str] = None) -> List[str]:
    """Every node data can reach from ``start`` by following flows, optionally of one class only. ``start`` is excluded."""
    graph: Dict[str, Set[str]] = defaultdict(set)
    for f in flows:
        if data_class is None or f.data_class == data_class:
            graph[f.src].add(f.dst)
    seen: Set[str] = set()
    queue = deque([start])
    while queue:
        for nxt in sorted(graph[queue.popleft()]):
            if nxt not in seen and nxt != start:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


def audit(nodes: Mapping[str, Node], flows: Iterable[Flow], residency: Residency, home: Optional[str] = None) -> Dict[str, object]:
    """One call, one result: violations, unclassified classes, exposure, and egress from ``home`` if given."""
    flows = list(flows)
    result: Dict[str, object] = {
        "violations": check(nodes, flows, residency),
        "unclassified": unclassified(flows, residency),
        "exposure": exposure(nodes, flows),
    }
    if home is not None:
        result["egress"] = egress(nodes, flows, home)
    return result


def render(nodes: Mapping[str, Node], flows: Iterable[Flow], residency: Residency, home: Optional[str] = None) -> str:
    """A Markdown residency summary. Readiness aid, not legal advice."""
    flows = list(flows)
    a = audit(nodes, flows, residency, home)
    violations: List[Violation] = a["violations"]  # type: ignore[assignment]
    lines = ["# Data residency summary", "", f"- Nodes: {len(nodes)}, flows: {len(flows)}", f"- Violations: {len(violations)}",
             "- Note: enforces the rules supplied, not what the law requires", ""]
    lines += ["## Violations", ""] + ([f"- {v.reason} ({v.flow.src} to {v.flow.dst})" for v in violations] or ["- None."])
    lines += ["", "## Exposure", ""] + [f"- {k}: {', '.join(v)}" for k, v in a["exposure"].items()]  # type: ignore[attr-defined]
    if a["unclassified"]:
        lines += ["", "## Unclassified data classes (no rule)", ""] + [f"- {c}" for c in a["unclassified"]]  # type: ignore[attr-defined]
    if home is not None:
        out: List[Flow] = a["egress"]  # type: ignore[assignment]
        lines += ["", f"## Leaves {home}", ""] + ([f"- {f.src} to {f.dst} ({f.data_class})" for f in out] or ["- Nothing."])
    return "\n".join(lines) + "\n"


def pair(nodes: Iterable[Node]) -> Tuple[Dict[str, Node], int]:
    """Index nodes by name, refusing duplicates. Returns (index, count)."""
    index: Dict[str, Node] = {}
    for n in nodes:
        if n.name in index:
            raise ValueError(f"duplicate node '{n.name}'")
        index[n.name] = n
    return index, len(index)
