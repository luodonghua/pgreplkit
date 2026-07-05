"""Initial-sync seed-LSN resume for physical-seed strategies (FR-48..50).

The load-bearing "exactly-once at the seed boundary" sequence:
  1. CREATE PUBLICATION on source, then create the logical slot (publication MUST come
     first — logical decoding replays from the slot's restart_lsn, so the publication
     must exist at that point) — BEFORE the snapshot/clone
  2. snapshot/clone + provision target (same engine); capture seed LSN on the target
  3. CREATE SUBSCRIPTION ... (copy_data=false, create_slot=false, enabled=false,
     slot_name=<slot>)
  4. pg_replication_origin_advance('pg_<suboid>', seed_lsn)
  5. ALTER SUBSCRIPTION ... ENABLE

Step 1 (publication + slot, in that order) is prepare_source(); steps 3-5 are
wire_seed_resume(). The snapshot/seed capture between them is orchestrated by the
caller (setup / aws.rds).
"""

from __future__ import annotations

import psycopg

from pgreplkit.core import sqlgen
from pgreplkit.core.connection import fetch_scalar
from pgreplkit.core.engine import EngineInfo
from pgreplkit.core.executor import Executor, Sql
from pgreplkit.core.plan import SlotSpec
from pgreplkit.errors import EngineUnsupported
from pgreplkit.logconf import get_logger

log = get_logger()


def prepare_source(
    executor: Executor, sconn: psycopg.Connection, spec: SlotSpec
) -> None:
    """Step 1 (BEFORE snapshot): create the publication, THEN the logical slot.

    Ordering is load-bearing: logical decoding replays from the slot's restart_lsn, so
    the publication must already exist at that point — otherwise pgoutput fails with
    "publication does not exist". Verified on PostgreSQL 16.
    """
    executor.run(sconn, sqlgen.create_publication(spec))
    if sconn is not None and fetch_scalar(
        sconn, "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (spec.name,)
    ):
        log.info("slot %s already exists on source; reusing", spec.name)
        return
    executor.run(sconn, sqlgen.create_logical_slot(spec.name))


def capture_seed_lsn(engine: EngineInfo, tconn: psycopg.Connection) -> str:
    """Step 2: capture the consistent seed LSN on the restored/cloned target (FR-49)."""
    sql = engine.seed_lsn_sql
    if sql is None:
        raise EngineUnsupported(
            "physical-seed requires RDS or Aurora (seed-LSN function unavailable)"
        )
    if "rds_tools" in sql:
        tconn.execute("CREATE EXTENSION IF NOT EXISTS rds_tools")
    lsn = fetch_scalar(tconn, sql)
    if lsn is None:
        raise EngineUnsupported("seed-LSN function returned NULL")
    return str(lsn)


def wire_seed_resume(
    executor: Executor,
    sconn: psycopg.Connection,
    tconn: psycopg.Connection,
    spec: SlotSpec,
    source_endpoint,
    seed_lsn: str,
) -> None:
    """Steps 3-6 (AFTER seed captured): disabled subscription, origin advance, enable.

    The publication + slot must already exist on the source (see prepare_source).
    """
    from pgreplkit.core.engine import detect_engine

    # disable_on_error is PG15+; gate it by the subscriber (target) version
    doe = detect_engine(tconn).features.disable_on_error if tconn is not None else True
    executor.run(
        tconn,
        sqlgen.create_subscription(
            spec, source_endpoint, copy_data=False, enabled=False, create_slot=False,
            disable_on_error=doe,
        ),
    )
    # resolve the replication-origin external_id (pg_<oid>) and advance it to the seed
    external_id = fetch_scalar(
        tconn, "SELECT 'pg_' || oid FROM pg_subscription WHERE subname = %s", (spec.name,)
    )
    if external_id is None:
        raise EngineUnsupported(f"subscription {spec.name} not found after creation")
    advance = Sql(
        "SELECT pg_replication_origin_advance(%s, %s)",
        params=(external_id, seed_lsn),
        note=f"advance origin {external_id} to seed LSN {seed_lsn} (exactly-once boundary)",
        target="target",
    )
    executor.run(tconn, advance)
    executor.run(tconn, sqlgen.enable_subscription(spec.name))
    log.info("wired seed-resume for %s from LSN %s", spec.name, seed_lsn)
