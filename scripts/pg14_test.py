"""Real RDS PostgreSQL 14 test: snapshot-restore seeded logical replication + version
gating. Proves the codebase works on a lower PG major version on real infra.
"""

from __future__ import annotations

import subprocess
import sys
import time

import boto3

from pgreplkit.aws import rds as awsrds
from pgreplkit.config.models import Endpoint, ExecutionMode
from pgreplkit.core.connection import connect, fetch_scalar
from pgreplkit.core.engine import detect_engine
from pgreplkit.core.executor import Executor
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec
from pgreplkit.phases.initial_sync import capture_seed_lsn, prepare_source, wire_seed_resume

PROFILE, REGION = "workshop", "us-east-1"
SRC_ID, TGT_ID = "pgrepl-pg14-src", "pgrepl-pg14-tgt"
SG, PGROUP, PW, DB = "sg-0a57b36d7093e1f5c", "pgrepl-logical-pg14", "PgreplKit2026!", "appdb"
SLOT = "pgrk_pg14_0"


def fresh_ip(host):
    o = subprocess.check_output(["dig", "+short", host], text=True).splitlines()
    return [x for x in o if x and x[0].isdigit()][-1]


def private_ip(ec2):
    enis = ec2.describe_network_interfaces(
        Filters=[{"Name": "vpc-id", "Values": ["vpc-96fc8aeb"]},
                 {"Name": "description", "Values": ["RDSNetworkInterface"]}]
    )["NetworkInterfaces"]
    return enis[0]["PrivateIpAddress"]


def ep(host, adv=None):
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres",
                    sslmode="require", advertised_host=adv, advertised_port=5432 if adv else None)


def main() -> int:
    rds = awsrds.RdsClient(PROFILE, REGION).client()
    ec2 = boto3.Session(profile_name=PROFILE, region_name=REGION).client("ec2")
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))

    print(">> waiting for PG14 source")
    src = awsrds.wait_available(rds, SRC_ID)
    source = ep(fresh_ip(awsrds.endpoint_of(src)[0]), adv=private_ip(ec2))
    print(f"   source public={source.host} private={source.advertised_host}")

    # version gating on real PG14
    with connect(source, "postgres", read_only=True) as c:
        engine = detect_engine(c)
    f = engine.features
    print(f"   engine={engine.kind} pg_major={f.major}")
    print(f"   features: truncate={f.truncate_replication} streaming={f.streaming} "
          f"row_filter={f.row_filter} parallel_apply={f.parallel_apply} "
          f"sub_needs_superuser={f.subscription_needs_superuser}")
    assert f.major == 14 and f.truncate_replication and f.streaming
    assert not f.row_filter and not f.parallel_apply and f.subscription_needs_superuser

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
        prepare_source(ex, c, spec)

    snap = f"{SRC_ID}-pgrk"
    awsrds.delete_snapshot(rds, snap)
    print(">> snapshot + restore PG14 target (several minutes)")
    awsrds.create_snapshot(rds, SRC_ID, snap)
    tgt = awsrds.restore_instance_from_snapshot(
        rds, snap, TGT_ID, instance_class="db.t3.micro",
        security_group_ids=[SG], parameter_group=PGROUP, publicly_accessible=True,
    )
    time.sleep(10)
    target = ep(fresh_ip(awsrds.endpoint_of(tgt)[0]))

    with connect(target, DB, autocommit=True) as tc:
        tengine = detect_engine(tc)
        seed_lsn = capture_seed_lsn(tengine, tc)  # rds_tools.logical_seed_lsn on PG14
        tc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
        tgt_seed = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
    print(f"   target seed_lsn={seed_lsn} rows={tgt_seed}")

    with connect(source, DB, autocommit=True) as c:
        c.execute("INSERT INTO t SELECT g,'post'||g FROM generate_series(51,70) g")
        src_total = int(fetch_scalar(c, "SELECT count(*) FROM t"))
    with connect(source, DB, autocommit=True) as sc, connect(target, DB, autocommit=True) as tc:
        wire_seed_resume(ex, sc, tc, spec, source, seed_lsn)

    ok = tt = dd = 0
    for _ in range(40):
        with connect(target, DB, read_only=True) as tc:
            tt = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
            dd = int(fetch_scalar(tc, "SELECT count(DISTINCT id) FROM t") or 0)
        if tt == src_total and dd == tt:
            ok = 1
            break
        time.sleep(3)
    print("=" * 60)
    print(f"PG14 RDS->RDS seed-resume {'PASS' if ok else 'FAIL'}: target {tgt_seed} -> {tt} "
          f"(source {src_total}), distinct={dd}")

    print(">> teardown target + snapshot")
    try:
        with connect(target, DB, autocommit=True) as tc:
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" DISABLE')
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" SET (slot_name = NONE)')
            tc.execute(f'DROP SUBSCRIPTION IF EXISTS "{SLOT}"')
    except Exception as exc:  # noqa: BLE001
        print(f"   cleanup warn: {exc}")
    awsrds.delete_instance(rds, TGT_ID)
    awsrds.delete_snapshot(rds, snap)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
