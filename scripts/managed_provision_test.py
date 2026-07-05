"""Managed E2E of the RDS->RDS seeded path using the exact call sequence run_setup uses.

Mirrors phases.setup._run_setup_physical_seed step-for-step (prepare_source -> boto3
snapshot/restore -> capture_seed_lsn -> wire_seed_resume) against real RDS, using
dig-resolved IPs to sidestep local DNS caching of reused RDS endpoint names.

  .venv/bin/python -u scripts/managed_provision_test.py
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
SRC_ID, TGT_ID = "pgrepl-src", "pgrepl-tgt"
SG, PGROUP, PW, DB = "sg-025fb2ff127919752", "pgrepl-logical-pg16", "PgreplKit2026!", "appdb"
SLOT = "pgrk_appdb_0"


def fresh_ip(host: str) -> str:
    """Resolve an A record via dig (bypasses the OS/mDNS cache)."""
    out = subprocess.check_output(["dig", "+short", host], text=True).strip().splitlines()
    ips = [ln for ln in out if ln and ln[0].isdigit()]
    if not ips:
        raise RuntimeError(f"could not resolve {host}")
    return ips[-1]


def private_ip(ec2, instance_hint: str) -> str:
    enis = ec2.describe_network_interfaces(
        Filters=[
            {"Name": "vpc-id", "Values": ["vpc-96fc8aeb"]},
            {"Name": "description", "Values": ["RDSNetworkInterface"]},
        ]
    )["NetworkInterfaces"]
    for eni in enis:
        if instance_hint in (eni.get("Description", "") + eni.get("PrivateDnsName", "")):
            return eni["PrivateIpAddress"]
    return enis[0]["PrivateIpAddress"]


def ep(host, adv=None):
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres",
                    advertised_host=adv, advertised_port=5432 if adv else None)


def main() -> int:
    rds = awsrds.RdsClient(PROFILE, REGION).client()
    ec2 = boto3.Session(profile_name=PROFILE, region_name=REGION).client("ec2")
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))

    print(">> waiting for source")
    src = awsrds.wait_available(rds, SRC_ID)
    src_dns = awsrds.endpoint_of(src)[0]
    src_pub, src_priv = fresh_ip(src_dns), private_ip(ec2, "")
    print(f"   source dns={src_dns} public={src_pub} private={src_priv}")
    # tool connects to source via fresh public IP; subscriber reaches it via private IP
    source = ep(src_pub, adv=src_priv)

    # seed source (50 rows) + prepare source (CREATE PUBLICATION then slot) BEFORE snapshot
    with connect(source, "postgres", autocommit=True) as c:
        if not fetch_scalar(c, "SELECT 1 FROM pg_database WHERE datname=%s", (DB,)):
            c.execute(f'CREATE DATABASE "{DB}"')
    with connect(source, DB, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS t")
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
        c.execute("INSERT INTO t SELECT g, 'pre'||g FROM generate_series(1,50) g")
        c.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
        c.execute("SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
                  "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)", (SLOT, SLOT))
        prepare_source(ex, c, spec)  # publication THEN slot (load-bearing)

    # boto3 snapshot + restore target (the provisioning run_setup._provision_target does)
    snap = f"{SRC_ID}-pgrk-e2e"
    awsrds.delete_snapshot(rds, snap)
    print(">> snapshot + restore target (several minutes)")
    awsrds.create_snapshot(rds, SRC_ID, snap)
    tgt = awsrds.restore_instance_from_snapshot(
        rds, snap, TGT_ID, instance_class="db.t4g.medium",
        security_group_ids=[SG], parameter_group=PGROUP, publicly_accessible=True,
    )
    tgt_dns = awsrds.endpoint_of(tgt)[0]
    time.sleep(10)
    target = ep(fresh_ip(tgt_dns))
    print(f"   target dns={tgt_dns} public={target.host}")

    # capture seed LSN on the restored target, then wire seed-resume
    with connect(target, DB, autocommit=True) as tc:
        engine = detect_engine(tc)
        seed_lsn = capture_seed_lsn(engine, tc)
        tc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')
        tgt_seed = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
    print(f"   engine={engine.kind} seed_lsn={seed_lsn} target_rows={tgt_seed}")

    with connect(source, DB, autocommit=True) as c:
        c.execute("INSERT INTO t SELECT g, 'post'||g FROM generate_series(51,70) g")
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
    print(f"{'PASS' if ok else 'FAIL'}: RDS->RDS seed-resume — target {tgt_seed} -> "
          f"{tgt_total} (source {src_total}), distinct={distinct}")

    print(">> teardown target + snapshot")
    try:
        with connect(target, DB, autocommit=True) as tc:
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" DISABLE')
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" SET (slot_name = NONE)')
            tc.execute(f'DROP SUBSCRIPTION IF EXISTS "{SLOT}"')
    except Exception as exc:  # noqa: BLE001
        print(f"   cleanup warning: {exc}")
    awsrds.delete_instance(rds, TGT_ID)
    awsrds.delete_snapshot(rds, snap)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
