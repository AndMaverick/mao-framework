# mao-framework

**Model, Action, Orchestration.** A dependency free Python framework that decides whether an AI system may do something before it does it, and keeps evidence of what it did.

In July 2025 an AI coding agent on Replit deleted a live production database during a code freeze. It had been told, repeatedly, to change nothing. The model made the call and the architecture let it, because nothing stood between the plan and the delete.

MAO is the part that stands there. Every step names what it needs, what it may touch and how much damage it could do. A policy answers before the step runs. A check on the output comes before anything downstream depends on it. Every decision lands in a log that breaks if anyone edits it afterwards.

```
python -m mao demo
ok=True spend=6.2 outputs={'retrieve': [...], 'draft': 'summary of 2 files', 'publish': 'published'}
audit entries=12 chain_valid=True head=9dd4123d4a356c22
```

## The three layers

A model nobody cleared for the risk, a tool with more permission than it needed, a pipeline with no check between steps: those are three different failures, so they get three layers that can each be tested alone.

| Layer | What it owns | Module |
|---|---|---|
| **Model** | Capability, cost, latency, and the highest risk a model is cleared for. Routing picks the cheapest model that qualifies. | `mao.model` |
| **Action** | What the system may do. Every action carries a scope, a risk rating, a side effect flag and, if you ask for it, a named owner. A policy decides before anything runs. | `mao.action` |
| **Orchestration** | Plans as dependency graphs. Every step is routed, gated, executed, verified and logged. | `mao.orchestrator`, `mao.audit` |

## Design rules

1. **Fail closed.** A step that needs a human and has no approver is denied. An approver that raises is a denial.
2. **Verify before you depend.** A step's output has to pass its check before any later step sees it.
3. **Bound everything.** Retries and spend are capped per run. Cycles and unknown dependencies are rejected before anything executes.
4. **Leave evidence.** Every gate decision, approval, retry and halt goes into a hash chained audit log. Editing or deleting one entry breaks verification at that entry.
5. **Be boring to deploy.** The core is the standard library, with no network and no hidden state. `mao.live` is the one module that opens connections, and only when you call it.

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

Twelve concrete questions, four per layer, show where an existing system will fail first.

```
python -m mao diagnose answers.json
```

`answers.json` maps question ids (`m1` to `o4`, listed in `mao.QUESTIONS`) to `true` or `false`. An unanswered question counts as no. The report names the weakest layer and lists every gap.

## What else is in the box

Each item began as a defect reproduced against an earlier version or as a control the earlier version lacked. The [changelog](CHANGELOG.md) has the history.

- **Failures that used to crash a run.** An approver or verifier that raises is a denial or a failed attempt. A side effecting step is attempted once unless you declare it idempotent, which is what prevents a duplicate payment after an ambiguous failure.
- **Money.** Every attempt is metered, failed ones included, in exact decimals from a price book you supply. Adapters read usage from Anthropic, OpenAI, DeepSeek, Moonshot (Kimi) and Gemini, so spend is counted one way whichever lab served the call. `mao export` writes a CSV that Token Ledger can analyse. `mao.live` calls the five providers on the standard library, retries what is worth retrying and never writes the key into an error or a log.
- **Evidence.** The audit log catches edits, deletions and reordering by itself. A full rewrite or a cut tail is caught against an anchor you store elsewhere. `mao.trace` attaches cost to the span that incurred it. `mao.router` learns which permitted model earns its price on your traffic.
- **Text going in.** `mao.guard` redacts personal data and scores prompt injection, with checksum validated card numbers and IBANs and Arabic and Persian digits handled. It is a heuristic filter, not a boundary, and says so at the top of the file.
- **Operating the system.** `mao.playbook` compiles markdown runbooks into plans and audits their approval gates against the policy that will run. `mao.assure` holds eval suites, a regression gate that names the cases that flipped while the average held, drift statistics and an incident register. `mao.context` packs retrieved chunks into a token budget. `mao.vault` versions prompts behind gated deploys.
- **Regions.** `mao.sovereign` checks data flows against residency rules you write, including derived classes and jurisdiction groups. `mao.lingua` normalises Arabic and Indonesian text and Gulf and rupiah amounts. `mao.compliance` scores EU AI Act, NIST AI RMF and GDPR controls from evidence, some of it read from the audit log itself. They are readiness aids and not legal advice.

## Handing work across a trust boundary

One agent reads text a stranger wrote. Another holds the tokens. What may pass between them?

`mao.handoff` allows a label from a fixed list, a severity from a fixed list and bounded line numbers, sealed by trusted code and turned into an effect by the privileged side's own table. There is no field for text, so there is nowhere for a hidden instruction to ride. A reader steered into a wrong but admitted value is still possible, and [docs/HANDOFF.md](docs/HANDOFF.md) lists that and six other limits beside the guarantees. The tests run every admitted value through a real gate.

## Reference scenario

`python examples/dispute_desk.py` runs a synthetic card dispute desk twice, ungoverned and governed, over the same 2,000 cases and the same faults, and prints every term of the comparison. The inputs are labelled synthetic, the prices are illustrative, and each dollar follows from the `ASSUMPTIONS` table at the top of the file. Change an assumption and the result changes. It demonstrates a method and is not evidence about any real institution.

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Threat model](docs/THREAT-MODEL.md) · [Handoff contract](docs/HANDOFF.md) · [Running live](docs/LIVE.md) · [Changelog](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Releasing](RELEASING.md)

**Read the thinking.** [The MAO Framework: How CFOs Should Actually Think About Enterprise AI](https://blog.andmaverick.com/the-mao-framework-how-cfos-should-actually-think-about-enterprise-ai) sets out the three layers. [Before Your Agent Touches Production, Give It an Owner](https://coderlegion.com/33962/before-your-agent-touches-production-give-it-an-owner) and [Nobody Signs for the Agent](https://blog.andmaverick.com/nobody-signs-for-the-agent) argue that every agent needs a named owner, which `Action.owner` and `Policy(require_owner=True)` enforce. [The Lethal Trifecta Test for Your Agent](https://coderlegion.com/34412/the-lethal-trifecta-test-for-your-agent) and [Closed Values: Hand Off Contracts for Untrusted Agents](https://coderlegion.com/35393/closed-values-hand-off-contracts-for-untrusted-agents) describe the split that the handoff contract builds.

## Tests

```
python -m unittest discover -s tests -t .
ruff check . && mypy
```

250 tests, many of them adversarial: hostile approvers and verifiers, retry storms, tampered, truncated and rewritten logs, malformed entries, unpriceable usage, formula injection in exports and a hundred admitted handoff values. CI runs Python 3.9 to 3.13, plus lint and strict type checking.

## Status

Version 0.5.0. Fit for evaluation, and for governing internal tooling where you review the scope and risk labels yourself, because a payment marked `risk=1, side_effects=False` bypasses the policy. Not audited by a third party. The provider adapters are tested against documented response shapes and `mao.live` against a local server that speaks each wire format. Neither has been run against a real provider account in this repository's CI. The control plane contains no model or tool code: you supply that through `Action.fn`.

## Author

Built by [AndMaverick](https://www.andmaverick.com). MIT licensed.

If a system you run can fail in ways that matter, the controls belong in the architecture and not in the postmortem. I am glad to talk through yours: [begin a conversation](https://calendly.com/andmaverick?utm_source=github&utm_medium=repo&utm_campaign=mao-framework).
