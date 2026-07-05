"""sync-sequences phase (FR-63): copy sequence values source -> target.

Sequences are not carried by logical replication. Must run AFTER writes stop on the
source (enforced by cutover ordering, FR-65), or target sequences could collide.
"""

from __future__ import annotations

from pgreplkit.config.models import ExecutionMode
from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.errors import ConfigError
from pgreplkit.logconf import get_logger

log = get_logger()


def run_sync_sequences(ctx: Context) -> int:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("sync-sequences requires a target endpoint")
    manifest = Manifest.load(default_manifest_path(cfg.project_name()))
    if manifest is None:
        raise ConfigError("no manifest found; run setup first")

    from pgreplkit.core.manifest import effective_endpoints

    src_ep, tgt_ep = effective_endpoints(cfg, manifest)

    dbs = sorted({s.db for s in manifest.slots})
    synced = 0
    for db in dbs:
        with connect(src_ep, db, read_only=True) as sc:
            seqs = catalog.list_sequences(sc)
            values = {seq: catalog.sequence_last_value(sc, seq) for seq in seqs}
        if ctx.mode is not ExecutionMode.EXECUTE:
            for seq, val in values.items():
                print(f"-- [{ctx.mode}] SELECT setval('{seq.qualified}', {val}, true);")
            synced += len(values)
            continue
        with connect(tgt_ep, db) as tc:
            for seq, val in values.items():
                if val is None:
                    continue
                # setval takes regclass — pass the quoted identifier so mixed-case /
                # special-char sequence names are not fold-cased (REVIEW M1).
                tc.execute("SELECT setval(%s, %s, true)", (seq.quoted, val))
                synced += 1
        log.info("%s: synced %d sequence(s)", db, len(values))
    return synced
