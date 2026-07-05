"""Managed Aurora-to-Aurora E2E: fast-clone seeded logical replication.

Mirrors the seed-LSN path (prepare_source pub+slot -> Aurora fast clone -> capture
aurora_volume_logical_start_lsn() -> wire_seed_resume) against a real Aurora PG cluster.
Uses dig-resolved public IPs (DNS-cache-safe) and the source writer's private IP as
advertised_host so the clone reaches the source intra-VPC.

  .venv/bin/python -u scripts/managed_aurora_test.py
"""

from __future__ import annotations

import subprocess
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
SRC_CLUSTER, TGT_CLUSTER = "pgrepl-aur-src", "pgrepl-aur-clone"
SG, CPG = "sg-0b903dd25b343f323", "pgrepl-aurora-logical"
PW, DB, SLOT = "PgreplKit2026!", "appdb", "pgrk_aur_appdb_0"
SRC_ENDPOINT = "pgrepl-aur-src.cluster-cfd3zhrzww2n.us-east-1.rds.amazonaws.com"
SRC_PRIV = "172.31.26.173"


def fresh_ip(host: str) -> str:
    out = subprocess.check_output(["dig", "+short", host], text=True).splitlines()
    ips = [ln for ln in out if ln and ln[0].isdigit()]
    if not ips:
        raise RuntimeError(f"cannot resolve {host}")
    return ips[-1]


def ep(host, adv=None):
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres",
                    advertised_host=adv, advertised_port=5432 if adv else None)


def main() -> int:
    rds = awsrds.RdsClient(PROFILE, REGION).client()
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))

    source = ep(fresh_ip(SRC_ENDPOINT), adv=SRC_PRIV)
    print(f">> source public={source.host} private={SRC_PRIV}")

    with connect(source, "postgres", autocommit=True) as c:
        if not fetch_scalar(c, "SELECT 1 FROM pg_database WHERE datname=%s", (DB,)):
            c.execute(f'CREATE DATABASE "{DB}"')
    with connect(source, DB, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS t")
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
        c.execute("INSERT INTO t SELECT g,'pre'||g FROM generate_series(1,50) g")
        c.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
        c.execute("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                  "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)", (SLOT, SLOT))
        prepare_source(ex, c, spec)   # publication THEN slot, BEFORE clone
    print("   seeded 50 rows + prepared source (pub+slot)")

    print(">> fast-cloning cluster (several minutes)")
    awsrds.delete_aurora_cluster(rds, TGT_CLUSTER)
    endpoint, _ = awsrds.clone_aurora_cluster(
        rds, SRC_CLUSTER, TGT_CLUSTER, instance_class="db.t4g.medium",
        security_group_ids=[SG], cluster_parameter_group=CPG, publicly_accessible=True,
    )
    time.sleep(10)
    target = ep(fresh_ip(endpoint))
    print(f"   clone endpoint={endpoint} public={target.host}")

    with connect(target, DB, autocommit=True) as tc:
        engine = detect_engine(tc)
        seed_lsn = capture_seed_lsn(engine, tc)
        tc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
        tgt_seed = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
    print(f"   engine={engine.kind} seed_lsn={seed_lsn} clone_rows={tgt_seed}")

    with connect(source, DB, autocommit=True) as c:
        c.execute("INSERT INTO t SELECT g,'post'||g FROM generate_series(51,70) g")
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
    print(f"{'PASS' if ok else 'FAIL'}: Aurora->Aurora fast-clone seed-resume — clone "
          f"{tgt_seed} -> {tgt_total} (source {src_total}), distinct={distinct}")

    print(">> teardown clone cluster")
    awsrds.delete_aurora_cluster(rds, TGT_CLUSTER)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
