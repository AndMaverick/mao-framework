# Architecture

MAO splits a governed AI system into three layers that can each be tested alone and that fail closed together.

```
            plan (steps + dependencies)
                      |
   +------------------v------------------+
   | Orchestration                        |
   |  order (graphlib) -> select model    |
   |  -> gate -> approve -> execute       |
   |  -> verify -> meter -> log           |
   +----+---------------+-----------+-----+
        |               |           |
   Model layer     Action layer   Audit log
   registry,       scope, risk,   hash chain,
   risk tiers,     side effects,  anchor
   cost, routing   policy, approval
```

## Model layer (`mao.model`)

A `ModelSpec` declares capabilities, `cost_per_1k` (dollars per 1,000 tokens), median latency, a `risk_tier` (1 to 3) and a context window. `ModelRegistry.select` returns the cheapest model that has every needed capability, is cleared for the action's risk, and meets any cost, latency or context limit. Ties break on latency, then name, so routing is deterministic and testable.

## Action layer (`mao.action`)

An `Action` has a scope, a risk (1 to 3) and a `side_effects` flag. A `Policy` grants a role a set of scopes and a risk ceiling, and sets `approval_risk`: side-effecting actions at or above it need a human. `Policy.check` returns a `Decision` before anything runs.

## Orchestration (`mao.orchestrator`)

`Orchestrator.run` takes `Step`s. Per step, in order:

1. Reject duplicate ids, unknown dependencies and cycles before executing anything.
2. Select a model for the step's needs and risk. No qualifying model halts the run.
3. Check the budget with one attempt of headroom.
4. Gate through the policy. A denied action halts the run.
5. If approval is required, call the approver. Only the value `True` approves. No approver, an exception, or any other value denies.
6. Execute. Failures are caught and recorded. A step with side effects and no `idempotent=True` declaration gets exactly one attempt, whatever `retries` says.
7. Run the step's `verify` check. A failed or raising check counts as a failed attempt, so unverified output never reaches a downstream step.
8. Meter the attempt. Actions report tokens through `ctx["report_usage"]`; with a `PriceBook`, the attempt is charged its exact decimal cost. Without usage, the model's flat `cost_per_1k` is charged and the entry is marked `estimated`. A missing price halts the run rather than guessing.

Every attempt is metered, including failed ones, because providers bill failed calls that consumed tokens.

## Audit log (`mao.audit`)

Each entry stores the previous entry's hash. `check()` returns a `Verification` with the first bad index and a reason. A hash chain alone cannot detect a rewrite of the whole log or truncation of its tail, so `RunResult.anchor` and `AuditLog.anchor()` return `{length, head}`. Store the anchor somewhere the log's writer cannot edit, and pass it back to `verify(head=..., length=...)`. Reserved field names (`seq`, `ts`, `event`, `prev`, `hash`) are refused so a caller cannot overwrite chain fields.

## Cost accounting (`mao.usage`, `mao.adapters`, `mao.export`)

`Usage` uses the same token semantics as Token Ledger: `input_tokens` is uncached input only, cached reads and cache writes are separate, output includes reasoning. `usage_from_anthropic`, `usage_from_openai`, `usage_from_deepseek`, `usage_from_moonshot` and `usage_from_gemini` read a provider response's usage block, and `usage_from(provider, response)` dispatches by name. Providers disagree about whether input includes cached tokens and where reasoning is counted, so each adapter normalises to the same shape and raises when counts contradict each other. They are duck-typed and tested against documented response shapes, not against live API calls. `write_usage_csv` turns the audit log into a CSV that `python -m tokenledger analyze --usage` reads, with estimated rows flagged.

## Content guard (`mao.guard`)

`redact` replaces personal data with typed placeholders. Matching runs on a digit-normalised copy of the same length and replacement runs on the original, so a message in any script keeps its own words. Cards need a valid Luhn checksum and IBANs need a valid mod-97 check, so arbitrary digit strings are left alone. `injection_score` folds compatibility forms and strips zero-width characters before scoring phrases in six languages. `ContentPolicy.check` returns a `Verdict`, and `guard_verifier` wraps a policy as a `Step.verify`, so an output that leaks data or carries an injection is a failed attempt and never reaches a dependent step. It is heuristic: a clean verdict means nothing obvious, not safe.

## Tracing and routing (`mao.trace`, `mao.router`)

`Tracer` records spans with durations, cost and errors. With `Orchestrator(tracer=...)` a run is a root span and each executed step a child span carrying its metered cost, so `total_cost()` equals the run's spend and `critical_path()` shows where the time went. `Router` is UCB1 with reward `quality - cost_weight * cost`. The registry and policy decide what is permitted. The router chooses among the permitted by what has paid off, and it is deterministic.

## Plans, assurance, context and prompts (`mao.playbook`, `mao.assure`, `mao.context`, `mao.vault`)

A playbook is a markdown runbook. `compile_playbook` parses it, `lint_playbook` checks it, and `playbook_to_spec` turns it into a plan. A `Plan` is a validated DAG with per-step cost, latency and approval declarations. Its critical path is the longest latency chain, and `audit` compares what the plan says needs approval with what the `Policy` will enforce, so a gate on paper cannot silently be absent in the run.

`assure` treats a model change like a code change. A `Suite` is scored by named scorers returning values in [0, 1]. `gate(baseline, candidate)` fails when a mean drops more than `max_drop`, and lists cases that went from passing to failing even when the mean held. Drift uses the population stability index (bins from baseline quantiles, 0.1 and 0.25 as the conventional thresholds), the Kolmogorov-Smirnov statistic, and a categorical PSI. The incident register scores impact x likelihood x (6 - detectability), maps the score to SEV1 to SEV4 and an SLA window, and reports breaches and mean time to resolve.

`pack` selects chunks greedily by score minus a diversity penalty (token overlap with what is already chosen) until the budget is spent. Pinned chunks are placed first and must fit. `PromptVault` stores prompt versions by content digest, never rewrites history (rollback is a new version), and gates deploys on a caller-supplied check. Each change is an `AuditLog` event.

## International layer (`mao.sovereign`, `mao.lingua`, `mao.compliance`)

Residency rules map a data class to the jurisdictions where it may be processed. A flow is checked at both ends, since data is processed where it leaves as well as where it lands. Two mechanisms prevent the common gaps. A derived class inherits the rules of what it contains, so a retrieval prompt assembled from personal data cannot be sent somewhere personal data cannot go. A class with no rule is reported by `unclassified`, because a flow that is unrestricted only by omission is how residency gaps start.

`lingua` is the mundane layer that makes regional input usable. Arabic text is normalised so one word written several ways matches itself. Money is parsed with `Decimal`, never float, and an amount without a stated currency raises. Indonesian slang is a small reviewable table, and words outside it, including names, pass through untouched.

`compliance` separates three states: met, missing and failed. A control is only as good as its evidence, so `runtime_evidence` takes audit logging from a log whose hash chain verifies and human oversight from a policy approval gate that the log shows was exercised. A claim that cannot be checked against the run stays a claim in the metadata, and the metadata is what a person typed.

## What MAO does not do

It does not call models, sandbox code, or inspect what an action actually does. Policy is only as good as the scope and risk labels you assign. See `THREAT-MODEL.md`.
