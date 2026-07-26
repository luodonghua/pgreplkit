"""sizing phase: pre-replication planning report (read-only).

For every in-scope table it reports storage footprint (table + indexes) and recent write
activity (inserts/updates/deletes per second since the last stats reset). This helps
choose an initial-sync method (logical copy vs. a physical seed for very large tables)
and anticipate steady-state replication lag. Strictly read-only (FR-31).

The row-building and formatting helpers are pure so they can be unit-tested without a
database.
"""

from __future__ import annotations

from dataclasses import dataclass

from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.catalog import TableSizing
from pgreplkit.core.connection import connect
from pgreplkit.core.matching import in_scope
from pgreplkit.core.topology import discover_topology


@dataclass(frozen=True)
class SizingRow:
    db: str
    qualified: str
    est_rows: int
    table_size: str
    index_count: int
    index_size: str
    total_size: str
    total_bytes: int
    ins_per_sec: float
    upd_per_sec: float
    del_per_sec: float


def human_bytes(n: int) -> str:
    """Human-readable byte size (binary units), e.g. 1536 -> '1.5 KiB'."""
    size = float(max(n, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"  # pragma: no cover


def _rate(count: int, seconds: float) -> float:
    return round(count / seconds, 3) if seconds > 0 else 0.0


def build_sizing_rows(
    db: str, sizings: list[TableSizing], seconds_since_reset: float
) -> list[SizingRow]:
    """Turn raw catalog sizings into display rows with per-second DML rates (pure)."""
    rows = [
        SizingRow(
            db=db,
            qualified=s.ref.qualified,
            est_rows=s.est_rows,
            table_size=human_bytes(s.table_bytes),
            index_count=s.index_count,
            index_size=human_bytes(s.index_bytes),
            total_size=human_bytes(s.total_bytes),
            total_bytes=s.total_bytes,
            ins_per_sec=_rate(s.inserts, seconds_since_reset),
            upd_per_sec=_rate(s.updates, seconds_since_reset),
            del_per_sec=_rate(s.deletes, seconds_since_reset),
        )
        for s in sizings
    ]
    rows.sort(key=lambda r: r.total_bytes, reverse=True)
    return rows


def run_sizing(ctx: Context) -> list[SizingRow]:
    cfg = ctx.config
    scope = cfg.scope
    topo = discover_topology(cfg.source, scope)

    all_rows: list[SizingRow] = []
    for ds in topo.in_scope:
        with connect(cfg.source, ds.name, read_only=True) as conn:
            relations = catalog.list_relations(conn)
            scoped_refs = {
                r.ref
                for r in relations
                if r.is_ordinary_table
                and in_scope(r.schema, scope.include_schemas, scope.exclude_schemas)
                and in_scope(r.name, scope.include_tables, scope.exclude_tables)
            }
            sizings = catalog.table_sizing(conn, scoped_refs)
            seconds = catalog.seconds_since_stats_reset(conn)
        all_rows.extend(build_sizing_rows(ds.name, sizings, seconds))

    from pgreplkit.report.render import render_sizing

    render_sizing(all_rows, json_output=ctx.json_output)
    return all_rows
