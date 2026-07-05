"""status phase: replication + initial-sync + slot/WAL health, per db/slot (FR-54..56).

Authoritative lag is bytes-behind off the source slot's confirmed_flush_lsn (FR-54).
"""

from __future__ import annotations

from dataclasses import dataclass

from pgreplkit.context import Context
from pgreplkit.core.connection import connect, fetch_all, fetch_one
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.errors import ConfigError


@dataclass
class SlotStatus:
    db: str
    name: str
    sub_enabled: bool | None
    tables_total: int
    tables_ready: int
    slot_active: bool | None
    wal_status: str | None
    lag_bytes: int | None

    @property
    def synced(self) -> bool:
        return self.tables_total > 0 and self.tables_ready == self.tables_total


def _slot_health(conn, slot_name: str) -> dict | None:
    return fetch_one(
        conn,
        """
        SELECT active,
               wal_status,
               pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint AS lag_bytes
        FROM pg_replication_slots WHERE slot_name = %s
        """,
        (slot_name,),
    )


def _sub_progress(conn, subname: str) -> dict:
    sub = fetch_one(
        conn, "SELECT oid, subenabled FROM pg_subscription WHERE subname = %s", (subname,)
    )
    if sub is None:
        return {"enabled": None, "total": 0, "ready": 0}
    rows = fetch_all(
        conn,
        "SELECT srsubstate FROM pg_subscription_rel WHERE srsubid = %s",
        (sub["oid"],),
    )
    total = len(rows)
    ready = sum(1 for r in rows if r["srsubstate"] == "r")
    return {"enabled": sub["subenabled"], "total": total, "ready": ready}


def gather_status(ctx: Context) -> list[SlotStatus]:
    cfg = ctx.config
    path = default_manifest_path(cfg.project_name())
    manifest = Manifest.load(path)
    if manifest is None:
        raise ConfigError(
            f"no manifest found at {path}", hint="run setup first, or check the project"
        )
    if cfg.target is None:
        raise ConfigError("status requires a target endpoint")

    from pgreplkit.core.manifest import effective_endpoints

    src_ep, tgt_ep = effective_endpoints(cfg, manifest)

    out: list[SlotStatus] = []
    by_db: dict[str, list] = {}
    for s in manifest.slots:
        by_db.setdefault(s.db, []).append(s)

    for db, slots in by_db.items():
        with connect(src_ep, db, read_only=True) as sconn, \
             connect(tgt_ep, db, read_only=True) as tconn:
            for rec in slots:
                prog = _sub_progress(tconn, rec.name)
                health = _slot_health(sconn, rec.name) or {}
                out.append(
                    SlotStatus(
                        db=db,
                        name=rec.name,
                        sub_enabled=prog["enabled"],
                        tables_total=prog["total"],
                        tables_ready=prog["ready"],
                        slot_active=health.get("active"),
                        wal_status=health.get("wal_status"),
                        lag_bytes=health.get("lag_bytes"),
                    )
                )
    return out


def run_status(ctx: Context) -> list[SlotStatus]:
    rows = gather_status(ctx)
    from pgreplkit.report.render import render_status

    render_status(rows, json_output=ctx.json_output)
    return rows
