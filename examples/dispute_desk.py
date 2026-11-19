"""Reference scenario: a card dispute desk, run ungoverned and governed over the same synthetic month.

Every input here is synthetic. No bank, vendor or customer is represented, and the prices are
illustrative figures chosen for this example, not any provider's list price. The point is the method:
each dollar in the result follows from the ASSUMPTIONS table below, and changing an assumption changes
the result. Run it:

    python examples/dispute_desk.py
    python examples/dispute_desk.py --seed 11 --cases 5000 --csv usage.csv

The desk handles a chargeback dispute in three steps: classify the dispute, draft the response to the
card network, submit it. Submitting issues a provisional credit to the cardholder, a real side effect
with real money behind it.

Both systems see the same disputes, the same amounts and the same faults, drawn once from a seeded
generator before either runs:

* draft failures: the model call fails after consuming tokens, then succeeds on a later attempt, or not at all.
* submit timeouts: the credit is committed at the bank, then the response is lost. The caller sees a
  timeout and cannot tell whether the credit happened.

Ungoverned: the premium model for every step (and, as a fairer comparison, the mid model for every
step), three attempts on any failure, no approval step, no audit trail.

Governed: MAO. The cheapest qualifying model per step, per-attempt metering, a side-effecting step is
never retried unless declared idempotent, approval is required for credits above a limit, and every
decision lands in a hash-chained audit log.
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from mao import (  # noqa: E402
    Action,
    AuditLog,
    ModelRegistry,
    ModelSpec,
    Orchestrator,
    Policy,
    PriceBook,
    Step,
    Usage,
    write_usage_csv,
)

ASSUMPTIONS: Dict[str, Any] = {
    "cases": 2000,
    "amount_lognormal": (4.5, 1.0),       # median about $90, long right tail; clipped to 5..5000
    "tokens_classify": {"input": 1400, "output": 80},
    "tokens_draft": {"input": 2200, "cached": 3800, "output": 650},
    "draft_failure_odds": [0.93, 0.05, 0.015, 0.005],  # P(0, 1, 2, 3 consecutive failures before success)
    "p_submit_timeout_after_commit": 0.03,
    "attempts_allowed": 3,
    "approval_limit_usd": 1000,           # governed: credits above this need a human
    "manual_case_usd": 12.0,              # one human review, or one reconciliation of an unknown credit
    "duplicate_recovery_rate": 0.60,      # share of a duplicate credit recovered by reversal
    "prices_per_million": {               # illustrative, USD per million tokens
        "small": {"input_per_million": 0.25, "output_per_million": 1.25},
        "mid": {"input_per_million": 3.0, "output_per_million": 15.0, "cached_input_per_million": 0.30},
        "premium": {"input_per_million": 15.0, "output_per_million": 75.0, "cached_input_per_million": 1.50},
    },
}


@dataclass
class Case:
    id: int
    amount: float
    draft_failures: int
    submit_timeout: bool


def make_cases(seed: int, n: int) -> List[Case]:
    rng = random.Random(seed)
    mu, sigma = ASSUMPTIONS["amount_lognormal"]
    odds = ASSUMPTIONS["draft_failure_odds"]
    cases = []
    for i in range(n):
        amount = round(min(5000.0, max(5.0, rng.lognormvariate(mu, sigma))), 2)
        roll, acc, fails = rng.random(), 0.0, 0
        for k, p in enumerate(odds):
            acc += p
            if roll < acc:
                fails = k
                break
        else:
            fails = len(odds) - 1
        cases.append(Case(i, amount, fails, rng.random() < ASSUMPTIONS["p_submit_timeout_after_commit"]))
    return cases


class Bank:
    """The system of record. A credit, once committed, stays committed whatever the caller sees."""

    def __init__(self) -> None:
        self.credits: List[Tuple[int, float]] = []

    def credit(self, case: Case, attempt: int) -> str:
        self.credits.append((case.id, case.amount))
        if case.submit_timeout and attempt == 1:
            raise TimeoutError("network response lost after commit")
        return f"ref-{case.id}"

    def duplicates(self) -> List[Tuple[int, float]]:
        seen: Dict[int, int] = {}
        extra: List[Tuple[int, float]] = []
        for cid, amount in self.credits:
            seen[cid] = seen.get(cid, 0) + 1
            if seen[cid] > 1:
                extra.append((cid, amount))
        return extra


def _usage(kind: str) -> Usage:
    t = ASSUMPTIONS["tokens_" + kind]
    return Usage(input_tokens=t["input"], output_tokens=t["output"], cached_input_tokens=t.get("cached", 0))


# ---------------------------------------------------------------- ungoverned

def run_ungoverned(cases: List[Case], model: str, prices: PriceBook) -> Dict[str, Any]:
    bank = Bank()
    spend, manual, unapproved_large = 0.0, 0, 0
    for case in cases:
        spend += prices.cost(model, _usage("classify"))
        done = False
        for attempt in range(1, ASSUMPTIONS["attempts_allowed"] + 1):
            spend += prices.cost(model, _usage("draft"))
            if attempt <= case.draft_failures:
                continue
            done = True
            break
        if not done:
            manual += 1
            continue
        for attempt in range(1, ASSUMPTIONS["attempts_allowed"] + 1):
            try:
                bank.credit(case, attempt)
                break
            except TimeoutError:
                continue
        if case.amount > ASSUMPTIONS["approval_limit_usd"]:
            unapproved_large += 1
    dups = bank.duplicates()
    return {
        "name": f"ungoverned ({model} for every step)", "llm_usd": spend, "manual_cases": manual,
        "duplicate_credits": len(dups), "duplicate_usd": sum(a for _, a in dups),
        "credits_over_limit_without_approval": unapproved_large, "unresolved_credits": 0, "audit": None,
    }


# ------------------------------------------------------------------ governed

def _within(amount: float, limit: float) -> Callable[[Step], bool]:
    return lambda step: amount <= limit


def run_governed(cases: List[Case], prices: PriceBook, csv_path: str = "") -> Dict[str, Any]:
    clock = itertools.count(1_700_000_000.0, 7.0).__next__
    audit = AuditLog(clock=clock)
    # cost_per_1k is USD per 1,000 tokens, the same unit as the price book. It ranks models for routing and
    # reserves headroom before each attempt; metered spend comes from the price book.
    registry = ModelRegistry([
        ModelSpec("small", frozenset({"classify"}), 0.0005, 150, 1, provider="synthetic"),
        ModelSpec("mid", frozenset({"classify", "draft"}), 0.006, 900, 2, provider="synthetic"),
        ModelSpec("premium", frozenset({"classify", "draft", "reason"}), 0.03, 2500, 3, provider="synthetic"),
        ModelSpec("ledger-api", frozenset({"submit"}), 0.0, 40, 3, provider="internal"),
    ])
    policy = Policy("dispute-desk", frozenset({"disputes:read", "disputes:write"}), max_risk=3, approval_risk=3)
    bank = Bank()
    spend = 0.0
    manual_review = reconcile = draft_dead = 0
    attempts_total = suppressed = 0
    limit = ASSUMPTIONS["approval_limit_usd"]

    for case in cases:
        draft_calls = itertools.count(1)

        def classify(ctx: Dict[str, Any], case: Case = case) -> str:
            ctx["report_usage"](_usage("classify"))
            return "fraud" if case.amount > 300 else "service"

        def draft(ctx: Dict[str, Any], case: Case = case, calls: Any = draft_calls) -> Dict[str, str]:
            attempt = next(calls)
            ctx["report_usage"](_usage("draft"))
            if attempt <= case.draft_failures:
                raise ConnectionError("model endpoint unavailable")
            return {"response": f"case {case.id}: {ctx['deps']['classify']}"}

        def submit(ctx: Dict[str, Any], case: Case = case) -> str:
            return bank.credit(case, 1 if not any(c == case.id for c, _ in bank.credits) else 2)

        steps = [
            Step("classify", Action("classify", "disputes:read", 1, False, classify), needs=frozenset({"classify"})),
            Step("draft", Action("draft", "disputes:read", 2, False, draft), needs=frozenset({"draft"}),
                 depends_on=("classify",), retries=ASSUMPTIONS["attempts_allowed"],
                 verify=lambda out: bool(out.get("response"))),
            Step("submit", Action("submit", "disputes:write", 3, True, submit), needs=frozenset({"submit"}),
                 depends_on=("draft",), retries=ASSUMPTIONS["attempts_allowed"]),
        ]
        orch = Orchestrator(registry, policy, audit, approver=_within(case.amount, limit),
                            prices=prices, workload="dispute-desk", team="disputes", budget=0.50)
        result = orch.run(steps)
        spend += result.spend
        attempts_total += result.attempts
        suppressed += result.retries_suppressed
        if result.ok:
            continue
        if result.failed_step == "submit" and result.reason and "approval" in result.reason:
            manual_review += 1
        elif result.failed_step == "submit":
            reconcile += 1
        else:
            draft_dead += 1

    if csv_path:
        write_usage_csv(audit, csv_path)
    dups = bank.duplicates()
    return {
        "name": "governed (MAO)", "llm_usd": spend, "manual_cases": manual_review + reconcile + draft_dead,
        "manual_review_over_limit": manual_review, "reconciliations": reconcile, "draft_dead": draft_dead,
        "duplicate_credits": len(dups), "duplicate_usd": sum(a for _, a in dups),
        "credits_over_limit_without_approval": 0, "unresolved_credits": reconcile,
        "retries_suppressed": suppressed, "audit": audit, "bank_credits": len(bank.credits),
    }


# -------------------------------------------------------------------- report

def net_effect(base: Dict[str, Any], gov: Dict[str, Any]) -> Dict[str, float]:
    """Dollar comparison between an ungoverned baseline and the governed run. Every term is explicit."""
    manual = ASSUMPTIONS["manual_case_usd"]
    loss_rate = 1 - ASSUMPTIONS["duplicate_recovery_rate"]
    llm_saved = base["llm_usd"] - gov["llm_usd"]
    dup_loss_avoided = (base["duplicate_usd"] - gov["duplicate_usd"]) * loss_rate
    manual_added = (gov["manual_cases"] - base["manual_cases"]) * manual
    return {
        "llm_saved": llm_saved, "duplicate_loss_avoided": dup_loss_avoided, "manual_cost_added": manual_added,
        "net": llm_saved + dup_loss_avoided - manual_added,
    }


def money(x: float) -> str:
    return f"${Decimal(str(round(x, 2))):,.2f}"


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cases", type=int, default=ASSUMPTIONS["cases"])
    ap.add_argument("--csv", default="", help="write the governed run's usage as a Token Ledger CSV")
    args = ap.parse_args(argv)

    prices = PriceBook(ASSUMPTIONS["prices_per_million"])
    cases = make_cases(args.seed, args.cases)
    gov = run_governed(cases, prices, args.csv)
    print(f"SYNTHETIC SCENARIO. seed={args.seed} cases={args.cases}. Not client data. Prices are illustrative.\n")
    for base in (run_ungoverned(cases, "premium", prices), run_ungoverned(cases, "mid", prices)):
        n = net_effect(base, gov)
        print(f"{base['name']}")
        print(f"  model spend {money(base['llm_usd'])}   duplicate credits {base['duplicate_credits']} "
              f"({money(base['duplicate_usd'])})   credits over limit with no approval {base['credits_over_limit_without_approval']}")
        print(f"  vs governed: model spend saved {money(n['llm_saved'])}, duplicate loss avoided "
              f"{money(n['duplicate_loss_avoided'])}, human cost added {money(n['manual_cost_added'])}, net {money(n['net'])}\n")
    print(f"governed (MAO)\n  model spend {money(gov['llm_usd'])}   duplicate credits {gov['duplicate_credits']}   "
          f"unsafe retries withheld {gov['retries_suppressed']}")
    print(f"  routed to human: {gov['manual_review_over_limit']} over the approval limit, "
          f"{gov['reconciliations']} unresolved credits to reconcile, {gov['draft_dead']} failed drafts")
    audit = gov["audit"]
    print(f"  audit entries {len(audit.entries)}, chain valid {audit.verify()}, anchor {audit.anchor()['head'][:16]}")
    print("\nWhat to change: edit ASSUMPTIONS. The conclusion is only as good as those numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
