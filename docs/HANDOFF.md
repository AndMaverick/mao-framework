# The handoff contract

An agent that reads text written by strangers should not be the same agent that holds your tokens. Split the work in two. One agent reads the outside world and holds nothing worth stealing. The other holds the tools and the data. Then the whole question of safety becomes one question: what is allowed to cross between them?

If the answer is "a validated JSON object", you have not answered it. A string field in a valid object is an injection channel with a type annotation. The sentence an attacker hid in a GitHub issue survives a trip through `"summary": "..."` as comfortably as it survives a trip through a paragraph.

This module is the narrowest answer I could defend. `mao/handoff.py`, with tests in `tests/test_handoff.py`.

## What crosses

The reader may return three things, and only three.

```json
{"label": "bug", "severity": "high", "lines": [12, 40]}
```

The label and severity are members of lists the privileged side owns. The lines are whole numbers inside a bound taken from the item itself. Nothing else is accepted. An extra key, a missing key, a float, a boolean, a duplicate key, a lookalike letter, a trailing space or a number out of range is refused with a fixed reason code. The refusal never repeats what it refused.

Everything else on the message is attached by trusted code, after validation: the reference to the item, a provenance name from a fixed list, a random nonce, a fingerprint of the vocabulary, and an HMAC tag over all of it.

```python
from mao import Policy, handoff as h

sealer = h.Sealer(key)                                   # trusted harness; the reader never sees the key
msg = sealer.seal_reader_output(raw_reply, ref="issue-17", max_line=issue.line_count)

gate = h.Gate(key, resolve=store.get, executors=executors, policy=Policy("triage", scopes), audit=log, owner="ana@example.org")
outcome = gate.handle(msg)       # Outcome(status="done", reason="ok", ref="issue-17", effects=("apply_label", "escalate"))
```

The message has no field that names an effect. The privileged side looks the pair of label and severity up in its own routing table, asks the Action layer whether the resulting effect is allowed, and only then runs it. Spam and anything at high severity or above go to a human. Posting a reply waits for an approver that must return exactly `True`.

## The claim, and how it is tested

The claim is not that the reader is honest. It is this: **every effect on the privileged side is harmless for every value the reader can produce.** An attacker who controls the reader's choice can pick any label and severity. The design makes that choice cost at most a label and a page to a human.

The set of admitted values is finite, so the claim is checked by running all of it. `domain()` yields every label and severity pair with the line shapes that matter, a hundred values for the default vocabulary. `EveryAdmittedValue` seals each one, runs it through a real gate and asserts that only catalogued effects ran, with arguments that are closed values, that escalation happened exactly where the schema says, and that nothing was posted without a human. A sample could miss a case. This cannot.

## Questions a careful reader will ask

| Question | Answer | Test class |
|---|---|---|
| Can the provenance tag be forged? | Not without the key. The tag covers every field, so changing the label, the severity, the lines, the reference, the nonce, the schema or the provenance breaks it. A second known provenance cannot be claimed either. | `CanTheTagBeForged` |
| Are caller supplied identifiers trustworthy? | The reader cannot supply one at all. The reference is attached by the harness, must match a plain pattern, and must resolve in the privileged side's own store. The line bound is derived again from that store, so an inflated bound on the sealing side is caught. | `CallerIdentifiers` |
| Can admitted values be manipulated into an unintended effect? | The reader can steer which admitted value is chosen. It cannot reach an effect outside the table. | `EveryAdmittedValue` |
| Are line number bounds enough? | They stop out of range and malformed numbers, and they cap the count. They do not make a chosen line harmless: see the first limit below. | `SchemaBypass` |
| Can the schema be bypassed with malformed input? | Six thousand seeded mutations of a valid reply either fail with a coded error or land inside the domain. Duplicate keys, NaN, floats, lookalike letters, lone surrogates, invalid UTF-8, a `str` subclass that lies about equality, and size or depth bombs are each refused. | `SchemaBypass` |
| Does the privileged side verify the action itself? | Yes. It validates the fields again after the tag check, looks the item up, derives the effect from its own table and asks the Policy. A policy that withholds a scope wins over a valid message. | `ThePrivilegedSideDecidesForItself` |
| Can escalation be abused? | It can be bounded, not prevented. A budget caps escalations per provenance and counts the overflow. Garbage and rejected messages never page a human and never spend the budget. | `CanEscalationBeAbused` |
| Does constrained output get mistaken for trustworthy output? | The documentation says plainly that it is not. See the limits. | `ConstrainedIsNotTrustworthy` |

## What this guarantees

- The reader's output cannot carry text to the privileged side. Whatever it writes either becomes one of a finite set of values or is refused with a code.
- A message that was not sealed by the key holder, or that was altered afterwards, is refused. A repeated message is refused while its nonce is remembered.
- The effect is chosen by the privileged side from its own table, then checked by the Policy. An approval gate still applies.
- Refusals, approvals and outcomes are written to the hash chained audit log using closed values and the reference, never the reader's text.

## What this does not guarantee

These are tested as behaviour, in tests named `test_documented_limit_...`, so this list cannot drift from the code.

1. **A chosen line is still a pointer into untrusted content.** If the privileged side fetches those lines and gives them to a model that has tools, the attack returns by another door. Executors in this module receive integers, never text. Keep it that way.
2. **A wrong value is accepted as a value.** A reader steered into labelling a real attack as `docs` gets `apply_label`, not a page to a human. The design bounds the cost of that mistake. It does not detect it. Sample accepted verdicts and review them.
3. **The tag proves who sealed the message, not that it is right.** Authenticity of the channel, not correctness of the content. It means something only if the reader runs in a different trust domain from the sealer and cannot read the key.
4. **A harness that shows the reader one item and seals the verdict against another is not detected.** The contract binds a verdict to a reference. It cannot see which item the reader read.
5. **Replay protection has a memory.** The guard remembers the most recent nonces. A replay older than that is accepted, so keep every executor idempotent per reference.
6. **Flooding is bounded, not prevented.** An attacker who controls the reader can spend the escalation budget on spam, and a real escalation in the same window is deferred. The deferral is counted and logged.
7. **A compromised host defeats all of it.** Whoever holds the key can seal anything. Whoever controls the privileged process can skip the gate.

## Using it

Put the sealer, the key and the reference lookup on the trusted side. Start with `TRIAGE`, or define a `Schema` with your own labels and severities. Keep the vocabulary small, because every addition multiplies the table you have to reason about. Write the executors so that each is harmless for any admitted value, and run `domain()` against the real gate in your own tests. Name an owner with `owner=` and set `Policy(require_owner=True)` so that an effect with no accountable person is denied.

Related reading: `docs/THREAT-MODEL.md` for the control plane as a whole, and `mao/guard.py` for the heuristic filter that sits in front of text before it reaches a model. That filter lowers the odds of an attack. This contract limits what a successful one can do.
