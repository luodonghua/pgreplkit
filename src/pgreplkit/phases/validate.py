"""validate phase (FR-61/61a/62): compare source and target beyond lag.

Always compares object counts (tables/sequences/large objects) and global objects
(roles). Optionally compares per-table row counts and checksums by depth.
"""

from __future__ import annotations

from pgreplkit.checks.results import CheckReport, CheckResult, Level
from pgreplkit.config.models import ValidateDepth
from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.core.model import TableRef
from pgreplkit.errors import ConfigError


def run_validate(ctx: Context, depth: ValidateDepth = ValidateDepth.SAMPLED) -> CheckReport:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("validate requires a target endpoint")
    manifest = Manifest.load(default_manifest_path(cfg.project_name()))
    if manifest is None:
        raise ConfigError("no manifest found; run setup first")

    from pgreplkit.core.manifest import effective_endpoints

    src_ep, tgt_ep = effective_endpoints(cfg, manifest)
    report = CheckReport()

    # --- global objects: roles (FR-61a / FR-32) --------------------------------------
    with connect(src_ep, src_ep.dbname or "postgres", read_only=True) as sc, \
         connect(tgt_ep, tgt_ep.dbname or "postgres", read_only=True) as tc:
        missing_roles = catalog.list_roles(sc) - catalog.list_roles(tc)
    if missing_roles:
        report.add(
            CheckResult(
                Level.BLOCK,
                "roles_missing",
                f"roles present on source but missing on target: {sorted(missing_roles)}",
                remediation="create the roles on the target (pgreplkit globals)",
            )
        )

    # --- per database: object counts + per-table rows --------------------------------
    dbs: dict[str, list[TableRef]] = {}
    for s in manifest.slots:
        dbs.setdefault(s.db, []).extend(TableRef.parse(t) for t in s.tables)

    for db, tables in dbs.items():
        with connect(src_ep, db, read_only=True) as sc, \
             connect(tgt_ep, db, read_only=True) as tc:
            _validate_object_counts(report, db, tables, sc, tc)
            if depth in (ValidateDepth.SAMPLED, ValidateDepth.FULL):
                _validate_row_counts(report, db, tables, sc, tc)
                _validate_checksums(report, db, tables, sc, tc,
                                    sample=depth == ValidateDepth.SAMPLED)

    if not report.blocks:
        report.add(CheckResult(Level.OK, "validate", "source and target match"))
    return report


def _validate_object_counts(report, db, tables, sc, tc) -> None:
    """Scope-aware object comparison (M5): verify each in-scope (manifest) table exists
    on the target, and compare sequence counts within the in-scope schemas only, so
    unrelated out-of-scope objects in a shared database do not cause false failures.
    """
    missing = [t for t in sorted(set(tables)) if not catalog.table_exists(tc, t)]
    if missing:
        report.add(
            CheckResult(
                Level.BLOCK,
                "count_tables",
                f"{db}: in-scope table(s) missing on target: "
                f"{[t.qualified for t in missing]}",
                remediation="pre-create the schema (pg_dump --schema-only | psql)",
                subject=db,
            )
        )
    schemas = {t.schema for t in tables}
    if schemas:
        s_seq = catalog.count_sequences(sc, schemas)
        t_seq = catalog.count_sequences(tc, schemas)
        if s_seq != t_seq:
            report.add(
                CheckResult(
                    Level.WARN,
                    "count_sequences",
                    f"{db}: sequence count differs in in-scope schemas — "
                    f"source={s_seq} target={t_seq} (sequences are not replicated; "
                    "sync at cutover)",
                    subject=db,
                )
            )


def _validate_row_counts(report, db, tables, sc, tc) -> None:
    for tref in sorted(set(tables)):
        sc_n = catalog.row_count(sc, tref)
        tc_n = catalog.row_count(tc, tref)
        if sc_n != tc_n:
            report.add(
                CheckResult(
                    Level.BLOCK,
                    "row_count",
                    f"{db}.{tref.qualified}: row count differs — "
                    f"source={sc_n} target={tc_n}",
                    subject=f"{db}.{tref.qualified}",
                )
            )


def _validate_checksums(report, db, tables, sc, tc, *, sample: bool) -> None:
    kind = "sampled" if sample else "full"
    for tref in sorted(set(tables)):
        s = catalog.table_checksum(sc, tref, sample=sample)
        t = catalog.table_checksum(tc, tref, sample=sample)
        if s != t:
            report.add(
                CheckResult(
                    Level.BLOCK,
                    "checksum",
                    f"{db}.{tref.qualified}: {kind} content checksum differs "
                    "(rows diverge despite matching counts)",
                    subject=f"{db}.{tref.qualified}",
                )
            )


def run_validate_cli(ctx: Context, depth: ValidateDepth) -> None:
    from pgreplkit.errors import ValidationFailed
    from pgreplkit.report.render import render_checks

    report = run_validate(ctx, depth)
    render_checks(report, json_output=ctx.json_output)
    if report.has_blocks:
        raise ValidationFailed(f"{len(report.blocks)} validation failure(s)")
