"""Managed-engine E2E: RDS->RDS snapshot-restore seeded logical replication.

Proves the exactly-once seed-LSN resume (FR-48..50) against real RDS PostgreSQL:
  seed data -> CREATE PUBLICATION + create slot (pub BEFORE slot) -> snapshot ->
  restore target -> capture logical_seed_lsn() -> insert post-snapshot rows on source
  -> wire seed-resume -> assert target converges to source exactly once.

Run manually (creates billable RDS; deletes the restored target + snapshot at the end):
  .venv/bin/python scripts/managed_seed_test.py

Networking note: the restored target must be able to reach the source publisher. With
public endpoints, allow both instances' public IPs on the shared security group (RDS
public endpoints can hairpin to public IPs within a VPC).
"""

from __future__ import annotations

import sys
import time

from pgreplkit.aws import rds as awsrds
from pgreplkit.config.models import Endpoint, ExecutionMode
from pgreplkit.core.connection import connect, fetch_scalar
from pgreplkit.core.engine import detect_engine
from pgreplkit.core.executor import Executor
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec
from pgreplkit.phases.initial_sync import capture_seed_lsn, prepare_source, wire_seed_resume

PROFILE, REGION = "workshop", "us-east-1"
SRC_ID, TGT_ID, SNAP_ID = "pgrepl-src", "pgrepl-tgt", "pgrepl-src-seedsnap"
PW, SG, DB, SLOT = "PgreplKit2026!", "sg-045db8c2d66fdf53d", "appdb", "pgrk_seed_appdb_0"


def ep(host: str) -> Endpoint:
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres")


def main() -> int:
    rc = awsrds.RdsClient(profile=PROFILE, region=REGION)
    rds = rc.client()
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))

    print(">> waiting for source")
    src = awsrds.wait_available(rds, SRC_ID)
    source = ep(awsrds.endpoint_of(src)[0])

    with connect(source, "postgres", autocommit=True) as c:
        if not fetch_scalar(c, "SELECT 1 FROM pg_database WHERE datname=%s", (DB,)):
            c.execute(f'CREATE DATABASE "{DB}"')
    with connect(source, DB, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS t")
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
        c.execute("INSERT INTO t SELECT g, 'pre'||g FROM generate_series(1,50) g")
        c.execute("DROP PUBLICATION IF EXISTS " + f'"{SLOT}"')
        c.execute("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                  "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)", (SLOT, SLOT))
        prepare_source(ex, c, spec)  # publication THEN slot (ordering is load-bearing)
        c.execute("INSERT INTO t SELECT g, 'preSnap'||g FROM generate_series(51,70) g")

    awsrds.delete_snapshot(rds, SNAP_ID)
    print(">> snapshot + restore target (several minutes)")
    awsrds.create_snapshot(rds, SRC_ID, SNAP_ID)
    tgt = awsrds.restore_instance_from_snapshot(
        rds, SNAP_ID, TGT_ID, instance_class="db.t4g.medium",
        security_group_ids=[SG], parameter_group="pgrepl-logical-pg16",
        publicly_accessible=True,
    )
    target = ep(awsrds.endpoint_of(tgt)[0])

    with connect(target, DB, autocommit=True) as tc:
        engine = detect_engine(tc)
        seed_lsn = capture_seed_lsn(engine, tc)
        tc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')  # restored copy, unused
    print(f"   seed_lsn={seed_lsn} engine={engine.kind}")

    with connect(source, DB, autocommit=True) as c:
        c.execute("INSERT INTO t SELECT g, 'postSnap'||g FROM generate_series(71,90) g")
        src_total = int(fetch_scalar(c, "SELECT count(*) FROM t"))

    with connect(source, DB, autocommit=True) as sc, connect(target, DB, autocommit=True) as tc:
        wire_seed_resume(ex, sc, tc, spec, source, seed_lsn)

    ok, tgt_total, distinct = False, 0, 0
    for _ in range(40):
        with connect(target, DB, read_only=True) as tc:
            tgt_total = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
            distinct = int(fetch_scalar(tc, "SELECT count(DISTINCT id) FROM t") or 0)
        if tgt_total == src_total and distinct == tgt_total:
            ok = True
            break
        time.sleep(3)

    print("=" * 60)
    verdict = "PASS" if ok else "FAIL"
    print(f"{verdict}: target={tgt_total} distinct={distinct} source={src_total}")

    print(">> cleanup")
    try:
        with connect(target, DB, autocommit=True) as tc:
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" DISABLE')
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" SET (slot_name = NONE)')
            tc.execute(f'DROP SUBSCRIPTION IF EXISTS "{SLOT}"')
        with connect(source, DB, autocommit=True) as sc:
            sc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
            sc.execute("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                       "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)", (SLOT, SLOT))
    except Exception as exc:  # noqa: BLE001
        print(f"   cleanup warning: {exc}")
    awsrds.delete_instance(rds, TGT_ID)
    awsrds.delete_snapshot(rds, SNAP_ID)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
