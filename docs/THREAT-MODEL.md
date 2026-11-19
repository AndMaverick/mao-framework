# Threat model

Scope: the control plane in this repository. MAO decides whether a step may run, records what happened, and makes tampering with the record detectable. It does not contain the code that steps run.

## Assets

- Side-effecting actions (payments, messages, writes) and the money or trust behind them.
- The audit record, as evidence for review.
- The budget.

## In scope, and what the tests show

| Threat | Control | Test |
|---|---|---|
| A buggy or malicious approver crashes the run or waves a step through | Approval granted only on `True`; exceptions deny and are logged | `test_adversarial` |
| A verifier that raises crashes the run | Counts as a failed attempt, logged with the error | `test_adversarial` |
| Retry after an ambiguous failure repeats a payment or message | Side-effecting steps get one attempt unless declared idempotent | `test_adversarial`, `test_dispute_desk` |
| Retries are free, so spend exceeds the budget | Every attempt is metered; the budget is checked before and after | `test_usage`, `test_adversarial` |
| A caller overwrites chain fields through event data | Reserved names refused | `test_adversarial` |
| Editing or deleting one log entry | Hash chain breaks at that entry, and reports its index | `test_adversarial` |
| Rewriting the whole log, or cutting off the tail | Not detectable from the log alone. Detected against an externally stored anchor | `test_adversarial` |
| Spreadsheet formula injection through exported text | Leading `= + - @` neutralised on export | `test_usage` |

## Out of scope, stated plainly

- **A compromised host.** Whoever controls the process can write any log they like. Store anchors off the host.
- **Mislabelled actions.** Marking a payment `risk=1, side_effects=False` bypasses the policy. Review labels like code.
- **Prompt injection inside a step.** MAO limits what an injected step may do (scope, risk, approval). It does not detect injection.
- **Provider billing.** The metered cost is computed from reported tokens and the prices you supply. Reconcile it to the invoice with Token Ledger.
- **Concurrency.** `AuditLog` is not thread-safe. One log per run, or serialise access.
- **Confidentiality.** Audit entries contain whatever you pass in. Do not log secrets or raw personal data.

## Reporting

Report vulnerabilities privately to the maintainer. Do not open a public issue for an unpatched weakness.
