"""teardown phase: drop subscriptions, slots, publications we created (FR-66..69).

Destructive — requires confirmation (NFR-3). Operates from the manifest; never drops
objects it does not recognize (FR-68). Detects orphaned slots that still retain WAL
(FR-69).
"""

from __future__ import annotations

from pgreplkit.config.models import ExecutionMode
from pgreplkit.context import Context
from pgreplkit.core import catalog, sqlgen
from pgreplkit.core.connection import connect
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.errors import ConfigError, ConfirmationRequired
from pgreplkit.logconf import get_logger

log = get_logger()


def run_teardown(ctx: Context, *, confirm: bool | None = None) -> Manifest:
    cfg = ctx.config
    path = default_manifest_path(cfg.project_name())
    manifest = Manifest.load(path)
    if manifest is None:
        raise ConfigError(f"no manifest found at {path}; nothing to tear down")
    if cfg.target is None:
        raise ConfigError("teardown requires a target endpoint")

    execute = ctx.mode is ExecutionMode.EXECUTE
    proceed = confirm if confirm is not None else ctx.assume_yes
    if execute and not proceed:
        raise ConfirmationRequired(
            f"teardown will drop {len(manifest.slots)} subscription(s)/publication(s)/slot(s)",
            hint="re-run with --yes to confirm",
        )

    from pgreplkit.core.executor import Executor
    from pgreplkit.core.manifest import effective_endpoints

    executor = Executor(ctx.mode)
    src_ep, tgt_ep = effective_endpoints(cfg, manifest)

    by_db: dict[str, list] = {}
    for s in manifest.slots:
        by_db.setdefault(s.db, []).append(s)

    for db, recs in by_db.items():
        if execute:
            with connect(tgt_ep, db) as tconn, connect(src_ep, db) as sconn:
                for rec in recs:
                    _teardown_slot(executor, rec, tconn, sconn)
        else:
            for rec in recs:
                _teardown_slot(executor, rec, None, None)

    # mark torn down and persist
    for s in manifest.slots:
        s.state = "torn_down"
    if execute:
        manifest.save(path)
    return manifest


def _teardown_slot(executor, rec, tconn, sconn) -> None:
    # 1) drop subscription on target (disable + detach slot first)
    for stmt in sqlgen.drop_subscription(rec.name):
        executor.run(tconn, stmt)
    # 2) drop publication on source
    executor.run(sconn, sqlgen.drop_publication(rec.name))
    # 3) drop the replication slot if it lingers on the source (FR-69). The SQL is
    #    guarded with WHERE EXISTS, so it is a no-op when the subscription already
    #    removed it; only warn when a slot is actually orphaned.
    if sconn is not None and catalog.fetch_scalar(
        sconn, "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (rec.name,)
    ):
        log.warning("orphaned slot %s still present on source; dropping (retains WAL)",
                    rec.name)
    executor.run(sconn, sqlgen.drop_replication_slot(rec.name))
