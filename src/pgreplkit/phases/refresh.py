"""refresh phase (FR-44): pick up tables added on the source after setup.

Adds newly in-scope tables to the appropriate publication (ALTER PUBLICATION ADD
TABLE) and refreshes the subscription. Warns that new tables must already exist on the
target.
"""

from __future__ import annotations

from pgreplkit.config.models import ExecutionMode
from pgreplkit.context import Context
from pgreplkit.core import catalog, sqlgen
from pgreplkit.core.connection import connect
from pgreplkit.core.executor import Executor
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.core.matching import in_scope
from pgreplkit.core.model import TableRef
from pgreplkit.errors import ConfigError
from pgreplkit.logconf import get_logger

log = get_logger()


def run_refresh(ctx: Context) -> int:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("refresh requires a target endpoint")
    manifest = Manifest.load(default_manifest_path(cfg.project_name()))
    if manifest is None:
        raise ConfigError("no manifest found; run setup first")

    from pgreplkit.core.manifest import effective_endpoints

    src_ep, tgt_ep = effective_endpoints(cfg, manifest)

    executor = Executor(ctx.mode)
    added = 0
    by_db: dict[str, list] = {}
    for s in manifest.slots:
        by_db.setdefault(s.db, []).append(s)

    for db, recs in by_db.items():
        # only the last slot of a db catches newly appeared tables (catch-all behavior)
        catch = recs[-1]
        with connect(src_ep, db) as sconn, connect(tgt_ep, db) as tconn:
            relations = catalog.list_relations(sconn)
            in_pub: set[TableRef] = set()
            for rec in recs:
                in_pub |= catalog.publication_tables(sconn, rec.name)
            for rel in relations:
                if rel.relkind not in ("r", "p") or rel.persistence != "p":
                    continue
                if rel.is_partition:  # covered by its partitioned root (FR-11)
                    continue
                if not in_scope(rel.schema, cfg.scope.include_schemas, cfg.scope.exclude_schemas):
                    continue
                if not in_scope(rel.name, cfg.scope.include_tables, cfg.scope.exclude_tables):
                    continue
                if rel.ref in in_pub:
                    continue
                log.warning(
                    "new table %s -> publication %s (must already exist on target)",
                    rel.ref.qualified, catch.name,
                )
                executor.run(sconn, sqlgen.alter_publication_add_table(catch.name, rel.ref))
                catch.tables.append(rel.ref.qualified)
                added += 1
            if added:
                for rec in recs:
                    executor.run(tconn, sqlgen.refresh_subscription(rec.name))

    if added and ctx.mode is ExecutionMode.EXECUTE:
        manifest.save(default_manifest_path(cfg.project_name()))
    return added
