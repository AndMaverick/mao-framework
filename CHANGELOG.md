# Changelog

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
