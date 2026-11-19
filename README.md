# mao-framework

**Model, Action, Orchestration.** A small, dependency-free Python framework for building AI systems that can be governed, audited, and trusted with real work.

Most AI failures are not model failures. They happen in the seams: a model nobody cleared for the risk, a tool with more permission than it needed, a pipeline with no check between steps. MAO makes each of those a first-class object you can test.

```
python -m mao demo
ok=True spend=6.2 outputs={'retrieve': [...], 'draft': 'summary of 2 files', 'publish': 'published'}
audit entries=12 chain_valid=True head=9dd4123d4a356c22
```

## The three layers

| Layer | What it owns | Module |
|---|---|---|
| **Model** | Capability, cost, latency, and the maximum risk a model is cleared for. Routing picks the cheapest model that qualifies. | `mao.model` |
| **Action** | What the system may do. Every action carries a scope, a risk rating, and a side-effect flag. A policy decides before anything runs. | `mao.action` |
| **Orchestration** | Plans as dependency graphs. Every step is routed, gated, executed, verified, and logged. | `mao.orchestrator`, `mao.audit` |

## Design rules

1. **Fail closed.** A step that needs human approval with no approver is denied, not waved through.
2. **Verify before you depend.** A step's output must pass its check before any downstream step sees it.
3. **Bound everything.** Retries and spend are capped per run. Cycles and unknown dependencies are rejected before execution.
4. **Leave evidence.** Every gate decision, approval, retry, and halt lands in a hash-chained audit log. Editing or deleting any entry breaks verification.
5. **Be boring to deploy.** Standard library only. No network, no hidden state, deterministic model selection.

## Quickstart

```python
from mao import (Action, AuditLog, ModelRegistry, ModelSpec,
                 Orchestrator, Policy, Step)

registry = ModelRegistry([
    ModelSpec("small", frozenset({"extract"}), 0.2, 120, risk_tier=1),
    ModelSpec("large", frozenset({"extract", "draft"}), 3.0, 900, risk_tier=2),
])
policy = Policy("analyst", frozenset({"read", "write"}), max_risk=2, approval_risk=2)

steps = [
    Step("draft",
         Action("draft", scope="read", risk=1, side_effects=False,
                fn=lambda ctx: "quarterly summary"),
         needs=frozenset({"draft"}),
         verify=lambda out: bool(out)),
    Step("publish",
         Action("publish", scope="write", risk=2, side_effects=True,
                fn=lambda ctx: "published"),
         needs=frozenset({"draft"}),
         depends_on=("draft",)),
]

audit = AuditLog()
orch = Orchestrator(registry, policy, audit,
                    approver=lambda step: input(f"approve {step.id}? ") == "y",
                    budget=10.0)
result = orch.run(steps)
assert audit.verify()
```

## The diagnostic

Score an existing system against twelve concrete questions, four per layer, and find the layer where it will fail first.

```
python -m mao diagnose answers.json
```

`answers.json` maps question ids (`m1` to `o4`, see `mao.QUESTIONS`) to `true` or `false`. Unanswered counts as no. The report names the weakest layer and lists every gap.

## What 0.2 adds

Every item here was a reproduced defect or a missing control in 0.1. See the [CHANGELOG](CHANGELOG.md).

- An approver or verifier that raises is a denial or a failed attempt, never a crash.
- Every attempt is metered, including failed ones, with exact decimal pricing from a price book you supply.
- A side-effecting step is attempted once unless you declare it idempotent. Duplicate payments from retries after an ambiguous failure are the failure this prevents.
- The audit log detects edits, deletions and reordering on its own, and detects full rewrites and truncation against a stored anchor.
- Usage adapters for Anthropic, OpenAI, DeepSeek, Moonshot (Kimi) and Google Gemini, so spend is counted the same way whichever lab served the call.
- `mao.guard` redacts personal data and scores prompt injection, with checksum-validated card and IBAN detection and Arabic and Persian digit handling. It is a heuristic filter, not a boundary, and plugs in as a step verifier.
- `mao.trace` gives every run a span tree with cost attached to the step that incurred it. `mao.router` learns which permitted model earns its price on your traffic.
- `mao.playbook` compiles markdown runbooks into plans with a critical path, and audits the plan's approval gates against the policy that will actually run. `mao.assure` holds eval suites, a regression gate that names the cases that flipped, drift statistics and an incident register with SLA tracking. `mao.context` packs retrieved chunks into a token budget. `mao.vault` versions prompts with gated deploys and an audit trail.
- `mao.sovereign` checks data flows against residency rules you define, including derived classes and jurisdiction groups. `mao.lingua` normalises Arabic and Indonesian text, Gulf and rupiah amounts. `mao.compliance` scores EU AI Act, NIST AI RMF and GDPR controls from evidence, some of it taken from the audit log itself. Built for regulated deployments in the Gulf and Southeast Asia. Readiness aids, not legal advice.
- `mao export` writes the usage record as a CSV that [Token Ledger](https://github.com/AndMaverick/token-ledger) can analyse.

## Reference scenario

`python examples/dispute_desk.py` runs a synthetic card dispute desk ungoverned and governed over the same 2,000 cases and the same faults, and prints every term of the comparison. Inputs are labelled synthetic, prices are illustrative, and each dollar follows from the `ASSUMPTIONS` table at the top of the file. Change an assumption and the result changes. It demonstrates the method. It is not evidence about any real institution.

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Threat model](docs/THREAT-MODEL.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Releasing](RELEASING.md)

## Tests

```
python -m unittest discover -s tests -t .
ruff check . && mypy
```

117 tests, including adversarial ones that try to defeat each control: hostile approvers and verifiers, retry storms, tampered, truncated and rewritten logs, malformed entries, unpriceable usage, and formula injection in exports. CI runs Python 3.9 to 3.13, plus lint and strict type checking.

## Status

Version 0.2.0. Suitable for evaluation and for governing internal tooling where you review the scope and risk labels yourself. Not yet audited by a third party, and the provider adapters are tested against documented response shapes, not live API calls. The control plane contains no model or tool code: you supply that through `Action.fn`.

## Author

Built by [AndMaverick](https://www.andmaverick.com). MIT licensed.

---

**Related reading:** [Nobody Signs for the Agent](https://blog.andmaverick.com/nobody-signs-for-the-agent?utm_source=github&utm_medium=repo&utm_campaign=mao-framework)

**If this is your problem.** If a system you run can fail in ways that matter, the controls belong in the architecture, not in the postmortem. I am happy to talk through yours. [Begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=mao-framework).
