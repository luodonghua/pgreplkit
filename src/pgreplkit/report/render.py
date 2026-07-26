"""Rendering of topology/discover results as a rich table or JSON (NFR-5)."""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from pgreplkit.core.topology import TopologyReport

_console = Console()


def topology_to_dict(report: TopologyReport) -> dict:
    return {
        "databases": [
            {
                "name": d.name,
                "in_scope": d.included,
                "reason": d.reason,
                "table_count": d.table_count,
                "schemas": d.schemas,
                "warnings": d.warnings,
            }
            for d in report.databases
        ],
        "summary": {
            "in_scope": len(report.in_scope),
            "skipped": len(report.skipped),
            "total_tables": sum(d.table_count for d in report.in_scope),
        },
    }


def render_topology(report: TopologyReport, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(topology_to_dict(report), indent=2))
        return

    table = Table(title="pgreplkit discover — cluster topology")
    table.add_column("database", style="cyan", no_wrap=True)
    table.add_column("scope")
    table.add_column("tables", justify="right")
    table.add_column("schemas")
    table.add_column("note", style="yellow")

    for d in report.databases:
        scope = "[green]in-scope[/green]" if d.included else "[dim]skipped[/dim]"
        note = d.reason or ("; ".join(d.warnings) if d.warnings else "")
        table.add_row(
            d.name,
            scope,
            str(d.table_count) if d.included else "-",
            ", ".join(d.schemas) if d.schemas else "-",
            note,
        )

    _console.print(table)
    _console.print(
        f"[bold]{len(report.in_scope)}[/bold] database(s) in scope, "
        f"[bold]{len(report.skipped)}[/bold] skipped, "
        f"[bold]{sum(d.table_count for d in report.in_scope)}[/bold] in-scope table(s)."
    )
    for d in report.in_scope:
        for w in d.warnings:
            _console.print(f"[yellow]![/yellow] {d.name}: {w}")


def checks_to_dict(report) -> dict:
    from pgreplkit.checks.results import CheckReport

    assert isinstance(report, CheckReport)
    return {
        "results": [
            {
                "level": r.level.label,
                "code": r.code,
                "subject": r.subject,
                "message": r.message,
                "remediation": r.remediation,
            }
            for r in report.results
        ],
        "summary": {
            "ok": len([r for r in report.results if r.level.label == "ok"]),
            "warn": len(report.warns),
            "block": len(report.blocks),
        },
    }


def render_checks(report, *, json_output: bool = False) -> None:
    from pgreplkit.checks.results import Level

    if json_output:
        print(json.dumps(checks_to_dict(report), indent=2))
        return

    table = Table(title="pgreplkit preflight — checks")
    table.add_column("level")
    table.add_column("code", style="cyan", no_wrap=True)
    table.add_column("subject", style="magenta")
    table.add_column("message")

    style = {Level.OK: "green", Level.WARN: "yellow", Level.BLOCK: "red"}
    order = {Level.BLOCK: 0, Level.WARN: 1, Level.OK: 2}
    for r in sorted(report.results, key=lambda r: (order[r.level], r.code)):
        table.add_row(
            f"[{style[r.level]}]{r.level.label.upper()}[/{style[r.level]}]",
            r.code,
            r.subject or "-",
            r.message,
        )

    _console.print(table)
    if not report.results:
        _console.print("[green]no issues found[/green]")
    _console.print(
        f"[red]{len(report.blocks)} block[/red], "
        f"[yellow]{len(report.warns)} warn[/yellow]."
    )
    for r in report.blocks + report.warns:
        if r.remediation:
            _console.print(f"  [dim]{r.code}:[/dim] {r.remediation}")


def render_sizing(rows, *, json_output: bool = False) -> None:
    """Render the pre-replication sizing/activity report (list[SizingRow])."""
    if json_output:
        print(
            json.dumps(
                {
                    "tables": [
                        {
                            "db": r.db,
                            "table": r.qualified,
                            "est_rows": r.est_rows,
                            "table_size": r.table_size,
                            "index_count": r.index_count,
                            "index_size": r.index_size,
                            "total_size": r.total_size,
                            "total_bytes": r.total_bytes,
                            "ins_per_sec": r.ins_per_sec,
                            "upd_per_sec": r.upd_per_sec,
                            "del_per_sec": r.del_per_sec,
                        }
                        for r in rows
                    ],
                    "summary": {
                        "tables": len(rows),
                        "total_bytes": sum(r.total_bytes for r in rows),
                        "total_dml_per_sec": round(
                            sum(r.ins_per_sec + r.upd_per_sec + r.del_per_sec for r in rows), 3
                        ),
                    },
                },
                indent=2,
            )
        )
        return

    table = Table(title="pgreplkit sizing — replication scope footprint & write activity")
    table.add_column("database", style="cyan")
    table.add_column("table", style="cyan")
    table.add_column("est rows", justify="right")
    table.add_column("table", justify="right")
    table.add_column("idx", justify="right")
    table.add_column("index", justify="right")
    table.add_column("total", justify="right")
    table.add_column("ins/s", justify="right")
    table.add_column("upd/s", justify="right")
    table.add_column("del/s", justify="right")

    for r in rows:
        table.add_row(
            r.db, r.qualified, f"{r.est_rows:,}", r.table_size, str(r.index_count),
            r.index_size, r.total_size,
            f"{r.ins_per_sec:g}", f"{r.upd_per_sec:g}", f"{r.del_per_sec:g}",
        )
    _console.print(table)
    if not rows:
        _console.print("[dim]no in-scope tables found[/dim]")
        return
    total_bytes = sum(r.total_bytes for r in rows)
    total_dml = sum(r.ins_per_sec + r.upd_per_sec + r.del_per_sec for r in rows)
    from pgreplkit.phases.sizing import human_bytes

    _console.print(
        f"[bold]{len(rows)}[/bold] table(s), total size [bold]{human_bytes(total_bytes)}[/bold], "
        f"combined write rate [bold]{total_dml:g}[/bold] rows/s "
        f"(since last stats reset — run ANALYZE first and sample twice for a true rate)."
    )


def render_status(rows, *, json_output: bool = False) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "slots": [
                        {
                            "db": r.db,
                            "slot": r.name,
                            "enabled": r.sub_enabled,
                            "tables_ready": r.tables_ready,
                            "tables_total": r.tables_total,
                            "slot_active": r.slot_active,
                            "wal_status": r.wal_status,
                            "lag_bytes": r.lag_bytes,
                            "synced": r.synced,
                        }
                        for r in rows
                    ]
                },
                indent=2,
            )
        )
        return

    table = Table(title="pgreplkit status — replication & slot health")
    table.add_column("database", style="cyan")
    table.add_column("slot", style="cyan")
    table.add_column("sub")
    table.add_column("init-sync", justify="right")
    table.add_column("slot")
    table.add_column("wal_status")
    table.add_column("lag(bytes)", justify="right")

    for r in rows:
        sub = "[green]enabled[/green]" if r.sub_enabled else "[red]disabled[/red]"
        sync = f"{r.tables_ready}/{r.tables_total}"
        sync_disp = f"[green]{sync}[/green]" if r.synced else f"[yellow]{sync}[/yellow]"
        slot_act = "[green]active[/green]" if r.slot_active else "[red]inactive[/red]"
        wal = r.wal_status or "-"
        wal_disp = f"[red]{wal}[/red]" if wal == "lost" else wal
        table.add_row(
            r.db, r.name, sub, sync_disp, slot_act, wal_disp,
            "-" if r.lag_bytes is None else str(r.lag_bytes),
        )
    _console.print(table)
