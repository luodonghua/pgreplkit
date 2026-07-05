"""Resume the PG14 seed test on the already-restored target (after disable_on_error fix)."""

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
from pgreplkit.phases.initial_sync import capture_seed_lsn, wire_seed_resume

PW, DB, SLOT = "PgreplKit2026!", "appdb", "pgrk_pg14_0"
SRC_ID, TGT_ID = "pgrepl-pg14-src", "pgrepl-pg14-tgt"
SRC_PRIV = "172.31.30.151"


def fresh_ip(h):
    o = subprocess.check_output(["dig", "+short", h], text=True).splitlines()
    return [x for x in o if x and x[0].isdigit()][-1]


def ep(host, adv=None):
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres",
                    sslmode="require", advertised_host=adv, advertised_port=5432 if adv else None)


def main() -> int:
    rds = awsrds.RdsClient("workshop", "us-east-1").client()
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))
    src = awsrds.wait_available(rds, SRC_ID)
    tgt = awsrds.wait_available(rds, TGT_ID)
    source = ep(fresh_ip(awsrds.endpoint_of(src)[0]), adv=SRC_PRIV)
    target = ep(fresh_ip(awsrds.endpoint_of(tgt)[0]))

    with connect(target, DB, autocommit=True) as tc:
        tc.execute(f'DROP SUBSCRIPTION IF EXISTS "{SLOT}"')
        engine = detect_engine(tc)
        seed_lsn = capture_seed_lsn(engine, tc)
        tgt_seed = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
    with connect(source, DB, read_only=True) as sc:
        src_total = int(fetch_scalar(sc, "SELECT count(*) FROM t") or 0)
    print(f"target seed_lsn={seed_lsn} target_rows={tgt_seed} source_rows={src_total}")

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

    print(">> teardown pg14 target + snapshot")
    try:
        with connect(target, DB, autocommit=True) as tc:
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" DISABLE')
            tc.execute(f'ALTER SUBSCRIPTION "{SLOT}" SET (slot_name = NONE)')
            tc.execute(f'DROP SUBSCRIPTION IF EXISTS "{SLOT}"')
    except Exception as exc:  # noqa: BLE001
        print(f"   warn: {exc}")
    awsrds.delete_instance(rds, TGT_ID)
    awsrds.delete_snapshot(rds, f"{SRC_ID}-pgrk")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
