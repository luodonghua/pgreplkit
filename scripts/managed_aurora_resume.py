"""Resume the Aurora seed test on the already-created clone (fixed engine detection)."""

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

PW, DB, SLOT = "PgreplKit2026!", "appdb", "pgrk_aur_appdb_0"
SRC = "pgrepl-aur-src.cluster-cfd3zhrzww2n.us-east-1.rds.amazonaws.com"
CLONE = "pgrepl-aur-clone.cluster-cfd3zhrzww2n.us-east-1.rds.amazonaws.com"
SRC_PRIV = "172.31.26.173"


def fresh_ip(h):
    o = subprocess.check_output(["dig", "+short", h], text=True).splitlines()
    return [x for x in o if x and x[0].isdigit()][-1]


def ep(host, adv=None):
    return Endpoint(host=host, port=5432, user="postgres", password=PW, dbname="postgres",
                    advertised_host=adv, advertised_port=5432 if adv else None)


def main() -> int:
    ex = Executor(ExecutionMode.EXECUTE)
    spec = SlotSpec(db=DB, index=0, name=SLOT, tables=(TableRef("public", "t"),))
    source, target = ep(fresh_ip(SRC), adv=SRC_PRIV), ep(fresh_ip(CLONE))

    with connect(target, DB, autocommit=True) as tc:
        engine = detect_engine(tc)
        print(f"engine detected on clone = {engine.kind} (seed sql: {engine.seed_lsn_sql})")
        tc.execute(f'DROP PUBLICATION IF EXISTS "{SLOT}"')  # cloned copy, unused
        seed_lsn = capture_seed_lsn(engine, tc)
        tgt_seed = int(fetch_scalar(tc, "SELECT count(*) FROM t") or 0)
    print(f"seed_lsn={seed_lsn} clone_rows={tgt_seed}")

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
    print(f"{'PASS' if ok else 'FAIL'}: Aurora->Aurora fast-clone seed-resume — "
          f"clone {tgt_seed} -> {tt} (source {src_total}), distinct={dd}")

    print(">> teardown clone")
    rds = awsrds.RdsClient("workshop", "us-east-1").client()
    awsrds.delete_aurora_cluster(rds, "pgrepl-aur-clone")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
