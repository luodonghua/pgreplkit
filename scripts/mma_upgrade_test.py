"""Clean forward (17->18) + reverse (18->17) logical replication test on the real mma
Aurora upgrade pair, using the tool's own prepare_source / wire_seed_resume with
consistent naming. Uses a fresh logical seed (pg_current_wal_lsn) since the clone is
already upgraded (can't re-clone).
"""

from __future__ import annotations

import json
import sys
import time

import boto3

from pgreplkit.config.models import Endpoint, ExecutionMode
from pgreplkit.core import sqlgen
from pgreplkit.core.connection import connect, fetch_scalar
from pgreplkit.core.executor import Executor
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec
from pgreplkit.phases.initial_sync import prepare_source, wire_seed_resume

DB = "pgrk_upg"
SRC = "mma-aurora-pg-cluster.cluster-cfd3zhrzww2n.us-east-1.rds.amazonaws.com"
CLONE = "mma-pgrk-clone.cluster-cfd3zhrzww2n.us-east-1.rds.amazonaws.com"
SRC_PRIV, CLONE_PRIV = "10.1.3.130", "10.1.4.206"
FWD, REV = "pgrk_up_fwd", "pgrk_up_rev"


def pw():
    sm = boto3.Session(profile_name="workshop", region_name="us-east-1").client("secretsmanager")
    secret = sm.get_secret_value(SecretId="MMA-secret-aurora-admin")["SecretString"]
    return json.loads(secret)["password"]


def ep(host, adv, password):
    return Endpoint(host=host, port=5432, user="postgres", password=password, dbname="postgres",
                    sslmode="require", advertised_host=adv, advertised_port=5432)


def _drop(conn, subs, pubs, slots):
    for s in subs:
        conn.execute(f'DROP SUBSCRIPTION IF EXISTS "{s}"')
    for p in pubs:
        conn.execute(f'DROP PUBLICATION IF EXISTS "{p}"')
    for sl in slots:
        conn.execute(
            "SELECT pg_drop_replication_slot(%s) WHERE EXISTS "
            "(SELECT 1 FROM pg_replication_slots WHERE slot_name=%s)",
            (sl, sl),
        )


def _safe_drop_sub(conn, name):
    try:
        conn.execute(f'ALTER SUBSCRIPTION "{name}" DISABLE')
        conn.execute(f'ALTER SUBSCRIPTION "{name}" SET (slot_name = NONE)')
    except Exception:  # noqa: BLE001
        pass
    conn.execute(f'DROP SUBSCRIPTION IF EXISTS "{name}"')


def main() -> int:
    password = pw()
    source = ep(SRC, SRC_PRIV, password)
    clone = ep(CLONE, CLONE_PRIV, password)
    ex = Executor(ExecutionMode.EXECUTE)

    # --- clean any leftover state from earlier runs --------------------------------
    with connect(clone, DB, autocommit=True) as tc:
        for s in ("pgrk_upg_slot", FWD, REV):
            _safe_drop_sub(tc, s)
        _drop(tc, [], ["pgrk_upg_pub", FWD, REV], [FWD, REV])
    with connect(source, DB, autocommit=True) as sc:
        for s in (FWD, REV):
            _safe_drop_sub(sc, s)
        _drop(sc, [], ["pgrk_upg_pub", FWD, REV], ["pgrk_upg_slot", FWD, REV])

    # ============ FORWARD 17 -> 18 (seed resume) ============
    spec = SlotSpec(db=DB, index=0, name=FWD, tables=(TableRef("public", "t"),))
    with connect(source, DB, autocommit=True) as sc:
        sc.execute("TRUNCATE t")
        sc.execute("INSERT INTO t (id,v) SELECT g,'seed'||g FROM generate_series(1,50) g")
        prepare_source(ex, sc, spec)  # pub FWD + slot FWD (pub-before-slot)
        seed_lsn = str(fetch_scalar(sc, "SELECT pg_current_wal_lsn()"))
        rows = list(sc.execute("SELECT id,v FROM t").fetchall())
    # logical seed: load the 50 rows onto the clone (as of the seed point)
    with connect(clone, DB, autocommit=True) as tc:
        tc.execute("TRUNCATE t")
        with tc.cursor() as cur:
            cur.executemany("INSERT INTO t (id,v) VALUES (%(id)s,%(v)s)", rows)
    # post-seed writes on source (17) after the seed point
    with connect(source, DB, autocommit=True) as sc:
        sc.execute("INSERT INTO t (id,v) SELECT g,'post'||g FROM generate_series(51,70) g")
        src_total = int(fetch_scalar(sc, "SELECT count(*) FROM t"))
    with connect(source, DB, autocommit=True) as sc, connect(clone, DB, autocommit=True) as tc:
        wire_seed_resume(ex, sc, tc, spec, source, seed_lsn)

    ok = tt = dd = 0
    for _ in range(40):
        with connect(clone, DB, read_only=True) as tc:
            tt = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
            dd = int(fetch_scalar(tc, "SELECT count(DISTINCT id) FROM t") or 0)
        if tt == src_total and dd == tt:
            ok = 1
            break
        time.sleep(3)
    print(f"FORWARD 17->18 {'PASS' if ok else 'FAIL'}: clone 50 -> {tt} "
          f"(source {src_total}), distinct={dd}, seed_lsn={seed_lsn}")
    if not ok:
        return 1

    # ============ REVERSE 18 -> 17 (rollback insurance) ============
    # both sides now identical (70 rows). Tear down forward, wire clone(18)->source(17).
    with connect(clone, DB, autocommit=True) as tc:
        _safe_drop_sub(tc, FWD)
    with connect(source, DB, autocommit=True) as sc:
        _drop(sc, [], [FWD], [FWD])

    rev_spec = SlotSpec(db=DB, index=0, name=REV, tables=(TableRef("public", "t"),))
    # clone (18) becomes publisher: pub + slot
    with connect(clone, DB, autocommit=True) as tc:
        prepare_source(ex, tc, rev_spec)
    # source (17) becomes subscriber: copy_data=false, enabled, streams from slot (no gap)
    with connect(source, DB, autocommit=True) as sc:
        ex.run(sc, sqlgen.create_subscription(rev_spec, clone, copy_data=False,
                                              enabled=True, create_slot=False))
    time.sleep(3)
    # write NEW rows on the 18 clone; they must appear on the 17 source
    with connect(clone, DB, autocommit=True) as tc:
        tc.execute("INSERT INTO t (id,v) SELECT g,'rev'||g FROM generate_series(71,80) g")
        clone_total = int(fetch_scalar(tc, "SELECT count(*) FROM t"))
    rok = st = 0
    for _ in range(40):
        with connect(source, DB, read_only=True) as sc:
            st = int(fetch_scalar(sc, "SELECT count(*) FROM t") or 0)
        if st == clone_total:
            rok = 1
            break
        time.sleep(3)
    print(f"REVERSE 18->17 {'PASS' if rok else 'FAIL'}: source {src_total} -> {st} "
          f"(clone {clone_total})")

    # cleanup replication artifacts (leave data; teardown of infra is separate)
    with connect(source, DB, autocommit=True) as sc:
        _safe_drop_sub(sc, REV)
    with connect(clone, DB, autocommit=True) as tc:
        _drop(tc, [], [REV], [REV])
    return 0 if (ok and rok) else 1


if __name__ == "__main__":
    sys.exit(main())
