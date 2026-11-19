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

`Usage` uses the same token semantics as Token Ledger: `input_tokens` is uncached input only, cached reads and cache writes are separate, output includes reasoning. `usage_from_anthropic` and `usage_from_openai` read a provider response's usage block. They are duck-typed and tested against documented response shapes, not against live API calls. `write_usage_csv` turns the audit log into a CSV that `python -m tokenledger analyze --usage` reads, with estimated rows flagged.

## What MAO does not do

It does not call models, sandbox code, or inspect what an action actually does. Policy is only as good as the scope and risk labels you assign. See `THREAT-MODEL.md`.
