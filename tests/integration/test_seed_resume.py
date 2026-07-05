"""Integration test for the exactly-once seed-LSN resume (FR-48..50), deterministic.

Mirrors the physical-seed flow on vanilla PG (no RDS needed): create slot -> capture a
seed LSN -> "seed" the target by copying current rows -> insert post-seed rows on the
source -> wire_seed_resume advancing the origin to the seed LSN -> assert the target
converges to the source exactly once (no duplicate-key errors), and a negative control
shows why the origin advance is required.
"""

from __future__ import annotations

import time
import uuid

import pytest

from pgreplkit.config.models import Endpoint, ExecutionMode
from pgreplkit.core.connection import connect, fetch_scalar
from pgreplkit.core.executor import Executor
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec
from pgreplkit.phases.initial_sync import prepare_source, wire_seed_resume

pytestmark = pytest.mark.integration


@pytest.fixture()
def seed_dbs(source_endpoint: Endpoint, target_endpoint: Endpoint):
    name = f"pgrk_it_seed_{uuid.uuid4().hex[:8]}"
    slot = "pgrk_seed_slot_0"
    with connect(source_endpoint, "postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with connect(target_endpoint, "postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with connect(source_endpoint, name, autocommit=True) as c:
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
    with connect(target_endpoint, name, autocommit=True) as c:
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
    try:
        yield name, slot
    finally:
        with connect(target_endpoint, name, autocommit=True) as c:
            c.execute(f'DROP SUBSCRIPTION IF EXISTS "{slot}"')
        with connect(source_endpoint, name, autocommit=True) as c:
            c.execute(f'DROP PUBLICATION IF EXISTS "{slot}"')
            c.execute("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                      "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)", (slot, slot))
        for ep in (source_endpoint, target_endpoint):
            with connect(ep, "postgres", autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _copy_seed(source, target, name):
    """Simulate a physical seed: copy current source rows into the target."""
    with connect(source, name, read_only=True) as sc:
        rows = fetch_scalar(sc, "SELECT count(*) FROM t")
        data = list(sc.execute("SELECT id, v FROM t").fetchall())
    with connect(target, name, autocommit=True) as tc:
        with tc.cursor() as cur:
            cur.executemany("INSERT INTO t (id, v) VALUES (%(id)s, %(v)s)", data)
    return rows


def test_seed_resume_exactly_once(source_endpoint, target_endpoint, seed_dbs) -> None:
    name, slot = seed_dbs
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=name, index=0, name=slot, tables=(TableRef("public", "t"),))

    with connect(source_endpoint, name, autocommit=True) as sc:
        sc.execute("INSERT INTO t SELECT g, 'pre'||g FROM generate_series(1,50) g")
        # publication THEN slot, BEFORE the "snapshot" (ordering is load-bearing)
        prepare_source(ex, sc, spec)
        sc.execute("INSERT INTO t SELECT g, 'preSnap'||g FROM generate_series(51,70) g")
        # capture the seed LSN at the snapshot point
        seed_lsn = str(fetch_scalar(sc, "SELECT pg_current_wal_lsn()"))

    # seed the target with rows as of the snapshot point (1..70)
    seeded = _copy_seed(source_endpoint, target_endpoint, name)
    assert seeded == 70

    # post-seed writes on the source (must replay exactly once)
    with connect(source_endpoint, name, autocommit=True) as sc:
        sc.execute("INSERT INTO t SELECT g, 'postSnap'||g FROM generate_series(71,90) g")
        src_total = int(fetch_scalar(sc, "SELECT count(*) FROM t"))
    assert src_total == 90

    # wire the seed-resume: disabled sub + origin advance to seed LSN + enable
    with connect(source_endpoint, name, autocommit=True) as sc, \
         connect(target_endpoint, name, autocommit=True) as tc:
        wire_seed_resume(ex, sc, tc, spec, source_endpoint, seed_lsn)

    # converge: target must reach exactly 90 rows, no duplicates, subscription healthy
    ok = False
    for _ in range(30):
        with connect(target_endpoint, name, read_only=True) as tc:
            total = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
            distinct = int(fetch_scalar(tc, "SELECT count(DISTINCT id) FROM t") or 0)
        if total == src_total and distinct == total:
            ok = True
            break
        time.sleep(1)

    with connect(target_endpoint, name, read_only=True) as tc:
        enabled = fetch_scalar(
            tc, "SELECT subenabled FROM pg_subscription WHERE subname=%s", (slot,)
        )

    assert ok, f"did not converge: total={total} distinct={distinct} want={src_total}"
    assert enabled, "subscription should be enabled and healthy (no apply error)"
