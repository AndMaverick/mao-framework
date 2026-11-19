# Changelog

## 0.4.0

Added, the international layer:
- `mao.sovereign`: data residency checks. Rules by data class, jurisdiction groups (`GCC`), derived classes (a prompt carrying personal data obeys the personal data rule), unclassified data classes reported instead of treated as free, transitive reach, egress from a home jurisdiction, and a Markdown summary. It enforces the rules you supply and is not legal advice.
- `mao.lingua`: Arabic normalisation (diacritics, tatweel, alef and yeh forms, Eastern digits and separators, optional teh marbuta), script profiling for routing mixed input, Gulf currency amounts in Latin and Arabic script, and Indonesian rupiah, slang, dates and phone numbers. Exact decimal arithmetic, and a missing currency is an error rather than a guess.
- `mao.compliance`: governance controls for the EU AI Act, NIST AI RMF and GDPR as met, missing or failed with the evidence written down. `runtime_evidence` derives audit logging and human oversight from the verified audit log and the policy, so an empty or tampered log, or an approval gate that never fired, does not count. Bias metrics (parity gap, disparate impact ratio, groups too small to trust) are computed from outcomes. A readiness aid, not legal advice.

## 0.3.0

Added:
- `mao.playbook`: markdown runbooks compile to plans. `Plan` gives order, parallel levels, total cost and critical path, binds to orchestrator `Step`s, and `Plan.audit` reports approval gates the plan declares but the policy will not enforce, gates the plan forgot, and steps the policy denies outright.
- `mao.assure`: eval suites with range-checked scorers (a crashing system scores zero and is recorded), a regression `gate` that also names cases that flipped from pass to fail while the mean held, drift measurement (PSI, KS, categorical PSI) and an incident register with risk scoring, SLA breach detection, MTTR and postmortems.
- `mao.context`: budgeted context packing with diversity weighting, pinned chunks and a pluggable token counter.
- `mao.vault`: a versioned prompt store. Commits are idempotent, rollback appends rather than rewrites, deploys are gated, and every change is chained into the audit log.

## 0.2.0

Fixes (each was reproduced against 0.1 before the change):
- An approver that raised crashed the run. It now denies, and the exception is logged.
- A verifier that raised crashed the run. It now counts as a failed attempt.
- Retries were charged once. Every attempt is now metered.
- A caller could pass event fields named `seq`, `ts`, `event`, `prev` or `hash` and corrupt the chain. They are refused.
- Rewriting the whole log, and truncating its tail, both verified as valid. Verification now accepts an external anchor (`head`, `length`) that detects both.
- A side-effecting step was retried after an ambiguous failure. It now gets one attempt unless declared `idempotent`.

Added:
- `PriceBook`, `Usage`, exact decimal pricing, `MissingPrice` instead of silent guesses.
- Provider usage adapters for Anthropic, OpenAI, DeepSeek, Moonshot (Kimi) and Google Gemini response shapes, with a `usage_from(provider, response)` dispatcher. Each normalises cached input, cache writes and reasoning the same way, and refuses counts that contradict each other.
- `mao.guard`: personal data redaction (email, IP, phone, card by Luhn, IBAN by ISO 13616 mod-97), digit normalisation for Arabic-Indic and Persian numerals, prompt-injection scoring in English, Arabic, Chinese, Spanish, French and Indonesian, `ContentPolicy`, and `guard_verifier` for use as `Step.verify`.
- `mao.trace`: `Tracer` span trees with cost rolled up from where it was incurred, critical path and error listing. Pass `tracer=` to `Orchestrator` for a root span per run and a child span per step.
- `mao.router`: `Router`, a UCB1 learning router over models that nets measured quality against metered cost. It chooses among models the registry and policy already permit.
- `mao export` and `write_usage_csv`, feeding Token Ledger.
- `mao verify` and `mao export` commands.
- `Verification` result with the failing index and reason.
- Dispute-desk reference scenario with inspectable assumptions (synthetic).
- Type hints throughout, `py.typed`, mypy strict and ruff in CI.

Changed:
- `ModelSpec` gains `provider`. `cost_per_1k` is documented as dollars per 1,000 tokens.

## 0.1.0

Initial release: model registry, policy, orchestrator, hash-chained audit log, diagnostic.
