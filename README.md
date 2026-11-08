# mao-framework

**Model, Action, Orchestration.** A small, dependency-free Python framework for building AI systems that can be governed, audited, and trusted with real work.

Most AI failures are not model failures. They happen in the seams: a model nobody cleared for the risk, a tool with more permission than it needed, a pipeline with no check between steps. MAO makes each of those a first-class object you can test.

```
python -m mao demo
ok=True spend=6.2 outputs={'retrieve': [...], 'draft': 'summary of 2 files', 'publish': 'published'}
audit entries=9 chain_valid=True
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

## Tests

```
python -m unittest discover -s tests -t . -v
```

The suite covers model routing, policy decisions, audit tamper and deletion detection, dependency flow, approval fail-closed behavior, retry recovery, verification halts, budget enforcement, and cycle rejection. CI runs it on every push, along with the demo.

## Status

Version 0.1. The core is small on purpose. Model and action implementations are yours to plug in through `Action.fn`, so the framework governs any provider or tool without depending on one.

## Author

Built by [AndMaverick](https://www.andmaverick.com). MIT licensed.
