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
