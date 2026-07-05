"""ready phase (FR-64): composite readiness gate for cutover.

Passes only when, for every slot: initial sync complete, slot active and not 'lost',
and apply lag within the configured byte threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from pgreplkit.context import Context
from pgreplkit.phases.status import SlotStatus, gather_status


@dataclass
class ReadyResult:
    passed: bool
    reasons: list[str]


def evaluate_ready(rows: list[SlotStatus], lag_threshold_bytes: int) -> ReadyResult:
    reasons: list[str] = []
    if not rows:
        reasons.append("no slots found in manifest")
    for r in rows:
        if not r.synced:
            reasons.append(f"{r.name}: initial sync incomplete ({r.tables_ready}/{r.tables_total})")
        # A missing source slot yields all-None health — must be a hard block, not a
        # silent pass (REVIEW H3). Otherwise cutover's drain would treat a dropped/lost
        # slot as zero-lag and proceed against a broken link.
        if r.slot_active is None or r.lag_bytes is None:
            reasons.append(f"{r.name}: source replication slot not found (dropped/lost?)")
            continue
        if r.slot_active is False:
            reasons.append(f"{r.name}: replication slot inactive")
        if r.wal_status == "lost":
            reasons.append(f"{r.name}: replication slot LOST (full resync required)")
        if r.lag_bytes > lag_threshold_bytes:
            reasons.append(
                f"{r.name}: lag {r.lag_bytes}B exceeds threshold {lag_threshold_bytes}B"
            )
    return ReadyResult(passed=not reasons, reasons=reasons)


def run_ready(ctx: Context) -> ReadyResult:
    rows = gather_status(ctx)
    result = evaluate_ready(rows, ctx.config.lag_threshold_bytes)
    if ctx.json_output:
        import json

        print(json.dumps({"ready": result.passed, "reasons": result.reasons}, indent=2))
    else:
        from rich.console import Console

        c = Console()
        if result.passed:
            c.print("[green]READY[/green] — all slots synced, active, within lag threshold")
        else:
            c.print("[red]NOT READY[/red]")
            for r in result.reasons:
                c.print(f"  [yellow]-[/yellow] {r}")
    return result
