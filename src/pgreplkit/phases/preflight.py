"""preflight phase: aggregate read-only eligibility & prerequisite checks (FR-14..29).

Runs discovery, then cluster-level source checks (version, logical WAL, slot headroom,
WAL retention) and per-database checks (replica identity, relation kinds, unreplicated
objects), plus target parity checks when a target is configured. Read-only (FR-31).
"""

from __future__ import annotations

from pgreplkit.checks import preflight_checks as pc
from pgreplkit.checks.results import CheckReport
from pgreplkit.config.models import InitSync, Scope, SlotStrategy
from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.engine import detect_engine
from pgreplkit.core.matching import in_scope
from pgreplkit.core.topology import DatabaseScope, discover_topology

_SOURCE_SETTINGS = [
    "wal_level",
    "max_replication_slots",
    "max_wal_senders",
    "max_worker_processes",
    "max_slot_wal_keep_size",
    "rds.logical_replication",
]

_TARGET_SETTINGS = [
    "max_logical_replication_workers",
    "max_worker_processes",
    "max_sync_workers_per_subscription",
    "max_replication_slots",
    "wal_level",
    "rds.logical_replication",
]


def _scoped_relations(relations: list[catalog.RelationInfo], scope: Scope):
    return [
        r
        for r in relations
        if in_scope(r.schema, scope.include_schemas, scope.exclude_schemas)
        and in_scope(r.name, scope.include_tables, scope.exclude_tables)
    ]


def _estimate_slots(ds: DatabaseScope, cfg) -> int:
    if cfg.strategy == SlotStrategy.SINGLE:
        return 1
    if cfg.strategy == SlotStrategy.PER_SCHEMA:
        return max(1, len(ds.schemas))
    return cfg.n  # balanced estimate


def _slot_demand(cfg, in_scope_dbs, report) -> int:
    """Total slot demand across in-scope databases (FR-13 headroom checks).

    For `manual`, the demand is read from the slot map (which may declare a different
    slot count than `--n`); a database absent from the map is a preflight block (M4).
    """
    if cfg.slots.strategy != SlotStrategy.MANUAL:
        return sum(_estimate_slots(ds, cfg.slots) for ds in in_scope_dbs)

    from pgreplkit.checks.results import CheckResult, Level

    try:
        from pgreplkit.config.slotmap import load_slot_map

        slot_map = load_slot_map(cfg.slots.slot_map)
    except Exception as exc:  # noqa: BLE001
        report.add(
            CheckResult(
                Level.BLOCK, "slot_map_unreadable",
                f"manual strategy: could not load slot map: {exc}",
                remediation="fix the --slot-map file",
            )
        )
        return sum(_estimate_slots(ds, cfg.slots) for ds in in_scope_dbs)

    demand = 0
    for ds in in_scope_dbs:
        db_slots = slot_map.get(ds.name)
        if db_slots is None:
            report.add(
                CheckResult(
                    Level.BLOCK, "slot_map_missing_db",
                    f"manual slot map has no entry for in-scope database '{ds.name}'",
                    remediation="add a 'databases.{db}.slots' section, or exclude the db",
                    subject=ds.name,
                )
            )
            continue
        demand += len(db_slots)
    return demand


def run_preflight(ctx: Context) -> CheckReport:
    cfg = ctx.config
    scope = cfg.scope
    report = CheckReport()

    topo = discover_topology(cfg.source, scope)
    in_scope_dbs = topo.in_scope

    # --- cluster-level source checks -------------------------------------------------
    entry_db = cfg.source.dbname or "postgres"
    with connect(cfg.source, entry_db, read_only=True) as conn:
        engine = detect_engine(conn)
        settings = catalog.get_settings(conn, _SOURCE_SETTINGS)
        current_slots = catalog.current_slot_count(conn)

    report.extend(pc.check_version(engine.features))
    report.extend(
        pc.check_source_logical_wal(
            engine.kind, settings.get("wal_level"), settings.get("rds.logical_replication")
        )
    )
    demand = _slot_demand(cfg, in_scope_dbs, report)
    report.extend(
        pc.check_source_params(
            settings, demand, current_slots, engine.is_managed
        )
    )
    report.extend(
        pc.check_wal_retention(int(settings.get("max_slot_wal_keep_size") or -1))
    )

    # --- target-side replication-parameter checks (FR-26) ----------------------------
    if cfg.target is not None:
        try:
            with connect(cfg.target, cfg.target.dbname or "postgres", read_only=True) as tconn:
                tengine = detect_engine(tconn)
                tsettings = catalog.get_settings(tconn, _TARGET_SETTINGS)
            report.extend(pc.check_target_params(tsettings, demand, tengine.is_managed))
        except Exception as exc:  # target unreachable — report, don't crash preflight
            from pgreplkit.checks.results import CheckResult, Level

            report.add(
                CheckResult(
                    Level.WARN, "target_params_unchecked",
                    f"could not check target replication parameters: {exc}",
                )
            )

    # --- per-database checks ---------------------------------------------------------
    for ds in in_scope_dbs:
        with connect(cfg.source, ds.name, read_only=True) as conn:
            relations = _scoped_relations(catalog.list_relations(conn), scope)
            seqs = catalog.count_sequences(conn)
            los = catalog.count_large_objects(conn)
            src_info = catalog.database_info(conn, ds.name)
            full_ri_unsafe = catalog.full_ri_unsafe_columns(conn)

        report.extend(pc.check_replica_identity(relations))
        report.extend(pc.check_relation_kinds(relations))
        report.extend(pc.check_unreplicated_objects(ds.name, seqs, los))

        # REPLICA IDENTITY FULL with non-comparable column types (scoped tables only)
        scoped_refs = {r.ref for r in relations}
        report.extend(
            pc.check_replica_identity_full_types(
                {ref: cols for ref, cols in full_ri_unsafe.items() if ref in scoped_refs}
            )
        )

        if cfg.target is not None and src_info is not None:
            report.extend(_target_parity(cfg, ds.name, src_info))
            # FR-18/19: target table/column compatibility for copy/pre-seeded strategies
            if cfg.init_sync in (InitSync.COPY, InitSync.NONE):
                report.extend(_target_schema(cfg, ds.name, relations))

    return report


def _target_schema(cfg, dbname: str, relations):
    from pgreplkit.checks.results import CheckResult, Level

    tables = [r.ref for r in relations if r.is_ordinary_table and r.persistence == "p"]
    if not tables:
        return []
    try:
        with connect(cfg.source, dbname, read_only=True) as sc, \
             connect(cfg.target, dbname, read_only=True) as tc:
            results = []
            for ref in tables:
                results.extend(
                    pc.check_target_columns(
                        dbname, ref.qualified,
                        catalog.table_columns(sc, ref),
                        catalog.table_columns(tc, ref),
                    )
                )
            return results
    except Exception as exc:  # target db unreachable — reported by _target_parity
        return [
            CheckResult(
                Level.WARN, "target_schema_unchecked",
                f"could not check target table/columns for '{dbname}': {exc}",
                subject=dbname,
            )
        ]


def _target_parity(cfg, dbname: str, src_info):
    from pgreplkit.checks.results import CheckResult, Level

    try:
        with connect(cfg.target, dbname, read_only=True) as tconn:
            tgt_info = catalog.database_info(tconn, dbname)
    except Exception as exc:  # connection or missing db
        return [
            CheckResult(
                Level.BLOCK,
                "target_db_missing",
                f"target database '{dbname}' is not reachable or does not exist: {exc}",
                remediation="pre-create the target database/schema (pg_dump --schema-only)",
                subject=dbname,
            )
        ]
    if tgt_info is None:
        return [
            CheckResult(
                Level.BLOCK,
                "target_db_missing",
                f"target database '{dbname}' does not exist",
                remediation="pre-create the target database and schema",
                subject=dbname,
            )
        ]
    return pc.check_encoding_parity(
        src_info.encoding,
        src_info.collate,
        src_info.ctype,
        tgt_info.encoding,
        tgt_info.collate,
        tgt_info.ctype,
    )
