# Running MAO live

`examples/live_dispute_desk.py` runs a governed card dispute desk against a real model and meters it from the
provider's own usage numbers.

```
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY, DEEPSEEK_API_KEY, MOONSHOT_API_KEY, GEMINI_API_KEY
cp prices.example.json prices.json  # fill in per-million prices for your model from the provider's pricing page
python examples/live_dispute_desk.py --provider anthropic --model <model id> --prices prices.json --budget 1.00
```

You supply the model id and the prices. A model with no price halts the run before any spend.

## What is real

The HTTP calls, the token counts (read from each response), the cost (counts times your prices), the hash-chained
audit log, the approval gate, the content guard and the accuracy against the known answers.

## What is simulated

The 12 disputes are synthetic and "submit" records a provisional credit in memory. No bank, network or customer
is touched.

## Checks the run makes on itself

- The cost the orchestrator metered during the run is recomputed from the per-call token counts. They must match.
- The audit log's hash chain must verify. The run exits non-zero if either check fails.
- Personal data is redacted before a model sees it, and an injection attempt (D11) never reaches a model.
- Credits above `--approval-limit` are denied and routed to a human queue.

## Reconciling to a provider

`calls.csv` lists each call with the provider's request id and token counts. Match them against the provider's
usage page, then reconcile the total against the invoice:

```
tokenledger analyze --usage live_run/usage.csv --prices prices.json --reconcile <invoice total>
```

The provider adapters are tested on documented response shapes and against a local server that speaks each wire
format. A live run is the only test of whether a provider has changed a field.
