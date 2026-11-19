"""Turn the audit log's usage events into rows Token Ledger can analyse.

``python -m tokenledger analyze --usage mao_usage.csv`` reads the file this writes: one row per
metered attempt, with date, model, workload, team, token counts and cost. Cost is taken from the
audit entry as recorded. Rows whose cost was a flat estimate rather than metered tokens are marked in
the ``estimated`` column so they are never mistaken for measured spend.
"""

from __future__ import annotations

import csv
import datetime as dt
from typing import Any, Dict, List

from .audit import AuditLog

FIELDS = ["date", "provider", "model", "workload", "team", "input_tokens", "cached_input_tokens",
          "cache_write_tokens", "output_tokens", "requests", "cost", "estimated", "outcome"]


def _safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def usage_rows(audit: AuditLog) -> List[Dict[str, Any]]:
    rows = []
    for e in audit.entries:
        if e.get("event") != "usage":
            continue
        day = dt.datetime.fromtimestamp(float(e["ts"]), tz=dt.timezone.utc).date().isoformat()
        rows.append({
            "date": day, "provider": e.get("provider", "unknown"), "model": e["model"],
            "workload": e.get("workload", "mao"), "team": e.get("team", "(unassigned)"),
            "input_tokens": e["input_tokens"], "cached_input_tokens": e["cached_input_tokens"],
            "cache_write_tokens": e["cache_write_tokens"], "output_tokens": e["output_tokens"],
            "requests": 1, "cost": e["cost"], "estimated": e["estimated"], "outcome": e["outcome"],
        })
    return rows


def write_usage_csv(audit: AuditLog, path: str) -> int:
    rows = usage_rows(audit)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: _safe(v) for k, v in row.items()})
    return len(rows)
