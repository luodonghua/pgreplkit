"""guide phase (FR-75..80): generate a manual runbook (SQL + notes), execute nothing.

Uses the same plan and SQL generators as setup, but renders an ordered, shareable
Markdown runbook with secrets redacted. Read-only inspection is allowed to tailor the
guide to the actual schema (FR-76). Covers scope, globals (roles/tablespaces), schema
pre-creation, replica-identity remediation, per-slot pub/sub (incl. physical-seed LSN),
monitoring, cutover, reverse, and teardown (FR-77).
"""

from __future__ import annotations

import secrets

from pgreplkit.checks import preflight_checks as pc
from pgreplkit.config.models import EngineKind, InitSync
from pgreplkit.context import Context
from pgreplkit.core import catalog, sqlgen
from pgreplkit.core.connection import connect
from pgreplkit.core.engine import detect_engine
from pgreplkit.core.matching import in_scope
from pgreplkit.core.plan import build_cluster_plan
from pgreplkit.core.topology import discover_topology
from pgreplkit.errors import ConfigError


def build_guide(ctx: Context) -> str:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("guide requires a target endpoint")

    run_id = f"guide_{secrets.token_hex(2)}"
    plan = build_cluster_plan(cfg, run_id)
    topo = discover_topology(cfg.source, cfg.scope)
    with connect(cfg.source, cfg.source.dbname or "postgres", read_only=True) as c:
        engine = detect_engine(c)

    out: list[str] = [
        "# pgreplkit — manual replication runbook",
        "",
        f"- source: `{cfg.source.host}:{cfg.source.port}` (engine: {engine.kind})",
        f"- target: `{cfg.target.host}:{cfg.target.port}`",
        f"- init-sync: `{cfg.init_sync}`  |  slots: `{cfg.slots.strategy}`",
        "",
    ]
    sections: list[tuple[str, list[str]]] = []

    # --- scope (discover / skip decisions) -------------------------------------------
    scope_lines = ["| database | scope | tables | note |", "|---|---|---|---|"]
    for d in topo.databases:
        scope_lines.append(
            f"| {d.name} | {'in-scope' if d.included else 'skipped'} | "
            f"{d.table_count if d.included else '-'} | {d.reason or ''} |"
        )
    sections.append(("Scope (discovered)", scope_lines))

    # --- prerequisites ---------------------------------------------------------------
    prereq = []
    if engine.kind in (EngineKind.RDS, EngineKind.AURORA):
        prereq += [
            "- Set `rds.logical_replication = 1` in the (cluster) parameter group "
            "(**static — requires a reboot**).",
            "- Connecting role needs `rds_replication` / `rds_superuser` "
            "(or `pg_create_subscription` on PG16+).",
        ]
    else:
        prereq.append("- Set `wal_level = logical` and restart the source.")
        if engine.features.subscription_needs_superuser:
            prereq.append("- On self-managed PG < 16, `CREATE SUBSCRIPTION` needs **superuser**.")
    prereq += [
        "- Ensure the target can reach the source (SG/firewall, pg_hba.conf).",
        "- Pre-create the target schema (logical replication does not copy DDL):",
        "  ```bash",
        "  pg_dump --schema-only -h SOURCE -d DB | psql -h TARGET -d DB",
        "  ```",
    ]
    sections.append(("Prerequisites", prereq))

    # --- global objects: roles + tablespaces -----------------------------------------
    sections.append(("Global objects (roles & tablespaces)", _globals_section(cfg)))

    # --- replica-identity remediation for flagged tables -----------------------------
    remediation = _replica_identity_section(cfg, topo)
    if remediation:
        sections.append(("Replica-identity remediation", remediation))

    # --- physical-seed LSN capture (snapshot/clone only) -----------------------------
    if cfg.init_sync in (InitSync.SNAPSHOT_RESTORE, InitSync.AURORA_FAST_CLONE):
        seed = [
            "Create the slot **before** the snapshot/clone, then capture the seed LSN:",
            "```sql",
            "-- on source, before snapshot (publication first, then slot):",
            "CREATE PUBLICATION <pub> FOR TABLE ...;",
            "SELECT pg_create_logical_replication_slot('<slot>', 'pgoutput');",
        ]
        if engine.kind == EngineKind.AURORA:
            seed.append("-- on the clone (Aurora->Aurora):")
            seed.append("SELECT aurora_volume_logical_start_lsn();")
        else:
            seed.append("-- on the restored target (RDS->RDS):")
            seed.append("CREATE EXTENSION IF NOT EXISTS rds_tools;")
            seed.append("SELECT rds_tools.logical_seed_lsn();")
        seed.append("```")
        sections.append(("Seed LSN (physical seed)", seed))

    # --- publications & subscriptions ------------------------------------------------
    copy_data = cfg.init_sync == InitSync.COPY
    pubsub: list[str] = []
    for dbp in plan.databases:
        pubsub.append(f"### database `{dbp.db}`")
        for warn in dbp.warnings:
            pubsub.append(f"> ⚠ {warn}")
        for spec in dbp.slots:
            pub = sqlgen.create_publication(spec)
            sub = sqlgen.create_subscription(
                spec, cfg.source, copy_data=copy_data, mask_password=True
            )
            pubsub += ["```sql", f"-- source: {pub.note}", pub.text + ";",
                       f"-- target: {sub.note}", sub.text + ";", "```"]
    sections.append(("Create publications & subscriptions", pubsub))

    # --- monitor / cutover / reverse / teardown --------------------------------------
    sections.append(("Monitor & validate", [
        "```sql",
        "-- lag on the source (bytes behind):",
        "SELECT slot_name, pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) AS lag,",
        "       active, wal_status FROM pg_replication_slots;",
        "-- initial-sync progress on the target:",
        "SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY 1;",
        "-- content validation before cutover (per in-scope table):",
        "--   SELECT md5(coalesce(string_agg(md5(t::text), ',' ORDER BY md5(t::text)),''))",
        "--   FROM <table> t;  -- compare source vs target",
        "```",
    ]))
    sections.append(("Cutover (ordered)", [
        "1. Stop writes on the source.",
        "2. Wait until lag reaches **0** on all slots.",
        "3. Sync sequences (AFTER writes stop): "
        "`SELECT setval('\"schema\".\"seq\"', <source last_value>, true);`",
        "4. Validate row counts + content checksums match.",
        "5. Switch application traffic to the target.",
    ]))
    sections.append(("Reverse (rollback insurance, green -> blue)", [
        "After cutover, keep the old source current for rollback:",
        "1. Keep writes **quiesced** on the new source (green) for the whole swap.",
        "2. Tear down the forward direction (drop subscription on green, "
        "publication + slot on blue).",
        "3. On green: `CREATE PUBLICATION <rev> FOR TABLE ...;` then "
        "`pg_create_logical_replication_slot('<rev>','pgoutput');`",
        "4. On blue: `CREATE SUBSCRIPTION <rev> CONNECTION '...' PUBLICATION <rev> "
        "WITH (copy_data=false, create_slot=false, slot_name='<rev>');`",
        "   (On PG16+ for bidirectional, add `origin = 'none'` to avoid loops.)",
        "5. To roll back: stop writes on green, confirm blue caught up, switch traffic to blue.",
    ]))
    teardown: list[str] = []
    for dbp in plan.databases:
        for spec in dbp.slots:
            teardown.append("```sql")
            for stmt in sqlgen.drop_subscription(spec.name):
                teardown.append(f"-- target: {stmt.note}\n{stmt.text};")
            teardown.append(f"-- source: {sqlgen.drop_publication(spec.name).text};")
            teardown.append("```")
    sections.append(("Teardown", teardown))

    # render with sequential numbering (no gaps)
    for i, (title, body) in enumerate(sections, start=1):
        out.append(f"## {i}. {title}")
        out.extend(body)
        out.append("")
    return "\n".join(out)


def _globals_section(cfg) -> list[str]:
    """Emit CREATE ROLE / GRANT and tablespace notes for what's missing on the target."""
    try:
        with connect(cfg.source, cfg.source.dbname or "postgres", read_only=True) as sc:
            src = catalog.role_details(sc)
            tspaces = catalog.non_default_tablespaces(sc)
        with connect(cfg.target, cfg.target.dbname or "postgres", read_only=True) as tc:
            tgt_roles = catalog.list_roles(tc)
    except Exception as exc:  # noqa: BLE001
        return [f"> (could not inspect roles/tablespaces: {exc})"]

    lines: list[str] = []
    missing = sorted(set(src) - tgt_roles)
    if missing:
        lines.append("```sql")
        for name in missing:
            d = src[name]
            attrs = "LOGIN" if d["can_login"] else "NOLOGIN"
            if d["createdb"]:
                attrs += " CREATEDB"
            if d["createrole"]:
                attrs += " CREATEROLE"
            lines.append(f'CREATE ROLE "{name}" WITH {attrs} PASSWORD \'<set-a-strong-password>\';')
        lines.append("```")
        lines.append("> Grants/memberships: reproduce with `pg_dumpall --roles-only` "
                     "filtered to these roles.")
    else:
        lines.append("- All required roles already exist on the target.")
    if tspaces:
        lines.append(f"- Non-default tablespaces in use: {sorted(tspaces)} — create them on "
                     "the target (`CREATE TABLESPACE ...`); on RDS/Aurora map objects to the "
                     "default tablespace (user tablespaces unsupported).")
    return lines


def _replica_identity_section(cfg, topo) -> list[str]:
    scope = cfg.scope
    lines: list[str] = []
    for ds in topo.in_scope:
        try:
            with connect(cfg.source, ds.name, read_only=True) as conn:
                rels = [
                    r for r in catalog.list_relations(conn)
                    if in_scope(r.schema, scope.include_schemas, scope.exclude_schemas)
                    and in_scope(r.name, scope.include_tables, scope.exclude_tables)
                ]
        except Exception:  # noqa: BLE001
            continue
        for res in pc.check_replica_identity(rels):
            lines.append(f"- `{ds.name}.{res.subject}` — {res.remediation}")
    return lines


def run_guide(ctx: Context) -> str:
    text = build_guide(ctx)
    print(text)
    return text
