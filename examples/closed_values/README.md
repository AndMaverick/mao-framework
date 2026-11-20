# Closed values: the small version

The code from the article "Closed Values: Hand Off Contracts for Untrusted Agents". Two files, standard library only, Python 3.9 or later, written to be read in one sitting.

```
python3 -m unittest -v test_handoff
```

Run that inside this directory. It runs eleven tests.

`handoff.py` has two functions. `parse` turns what a quarantined agent returned into a `Verdict` made of closed values, or raises `Rejected`. `plan` maps a `Verdict` to effects drawn from a fixed list. There is no field for free text, so there is nowhere for a hidden instruction to travel.

One deliberate difference from a plain reading of the standard library: the size check encodes with `surrogatepass`, so a lone surrogate in the output is refused with a reason and does not raise a `UnicodeEncodeError`. A test covers it.

## Where this stops

This version proves nothing about who produced a message. Anything that can call `parse` can call it with a string it wrote. The hardened version, [`mao/handoff.py`](../../mao/handoff.py), adds a message sealed by trusted code, a privileged side that checks the seal and looks the item up itself, replay protection, an escalation budget and an audit trail. [`docs/HANDOFF.md`](../../docs/HANDOFF.md) lists what both versions guarantee and what neither does.
