"""setup phase: create publications/subscriptions/slots per the plan (FR-36..45).

Runs a preflight gate first (FR-38). Idempotent with conflict detection: existing
publications/subscriptions are adopted only if their catalog facts match the plan
(table set, publish ops, publish_via_partition_root, slot name, subscribed
publications, connection target), else a PlanConflict is raised (FR-41).

Preflight blocks are classified: *correctness/data-loss* blocks (replica identity,
relation kind, target schema/column, encoding, version) require the stronger
``--force-correctness`` override, while capacity/environment blocks may be overridden
with ``--force``. Every override is recorded in the manifest (FR-74).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from pgreplkit.checks.results import CheckResult
from pgreplkit.config.models import EngineKind, ExecutionMode, InitSync, ProvisionMode
from pgreplkit.context import Context
from pgreplkit.core import catalog, reconcile, sqlgen
from pgreplkit.core.connection import connect
from pgreplkit.core.executor import Executor
from pgreplkit.core.manifest import Manifest, SlotRecord, default_manifest_path
from pgreplkit.core.plan import ClusterPlan, DatabasePlan, SlotSpec, build_cluster_plan
from pgreplkit.errors import ConfigError, ConfirmationRequired, PlanConflict, PreflightBlocked
from pgreplkit.logconf import get_logger

log = get_logger()

# Blocking preflight codes that indicate correctness / data-loss risk. These are NOT
# bypassed by a plain --force; they need the explicit --force-correctness override.
CORRECTNESS_BLOCK_CODES = frozenset(
    {
        "replica_identity",
        "relation_kind",
        "target_table_missing",
        "target_columns_mismatch",
        "target_db_missing",
        "encoding_mismatch",
        "version",
    }
)


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(2)


def _classify(block: CheckResult) -> str:
    return "correctness" if block.code in CORRECTNESS_BLOCK_CODES else "capacity"


def _gate_preflight(report, *, force: bool, force_correctness: bool) -> list[dict]:
    """Enforce the preflight gate and return the records of any overridden blocks.

    Capacity/environment blocks may be overridden with ``force``; correctness/data-loss
    blocks require ``force_correctness``. Raises PreflightBlocked otherwise (FR-38).
    """
    if not report.has_blocks:
        return []
    correctness = [b for b in report.blocks if _classify(b) == "correctness"]
    capacity = [b for b in report.blocks if _classify(b) == "capacity"]
    overridden: list[CheckResult] = []

    if capacity and not force:
        raise PreflightBlocked(
            f"{len(capacity)} capacity/environment block(s): "
            + "; ".join(b.code for b in capacity)
            + " — resolve them or re-run with --force",
        )
    overridden.extend(capacity)

    if correctness and not force_correctness:
        raise PreflightBlocked(
            f"{len(correctness)} CORRECTNESS/data-loss block(s): "
            + "; ".join(b.code for b in correctness)
            + " — these risk data loss (missing replica identity, schema/column "
            "mismatch, encoding, unsupported version). Fix them, or if you truly "
            "understand the risk re-run with --force-correctness",
        )
    overridden.extend(correctness)

    return [
        {
            "code": b.code,
            "level": b.level.label,
            "category": _classify(b),
            "subject": b.subject,
            "message": b.message,
        }
        for b in overridden
    ]


def _record_overrides(manifest: Manifest, overrides: list[dict]) -> None:
    if not overrides:
        return
    manifest.overrides = overrides
    for o in overrides:
        log.warning("preflight override (%s): %s — %s", o["category"], o["code"],
                    o["message"])


def run_setup(
    ctx: Context,
    *,
    force: bool = False,
    force_correctness: bool = False,
    confirm_provision: bool = False,
) -> Manifest:
    cfg = ctx.config
    if cfg.target is None and cfg.provision_mode.value != "provision":
        raise ConfigError("setup requires a target endpoint", hint="set target in config")

    if cfg.init_sync in (InitSync.SNAPSHOT_RESTORE, InitSync.AURORA_FAST_CLONE):
        return _run_setup_physical_seed(
            ctx, force=force, force_correctness=force_correctness,
            confirm_provision=confirm_provision,
        )

    if cfg.target is None:
        raise ConfigError("setup requires a target endpoint", hint="set target in config")

    # --- preflight gate (FR-38) ------------------------------------------------------
    from pgreplkit.phases.preflight import run_preflight

    report = run_preflight(ctx)
    overrides = _gate_preflight(report, force=force, force_correctness=force_correctness)

    # --- reuse or create manifest (idempotency, FR-41/74) ----------------------------
    project = cfg.project_name()
    path = default_manifest_path(project)
    manifest = Manifest.load(path) or Manifest(
        project=project,
        run_id=_new_run_id(),
        source=f"{cfg.source.host}:{cfg.source.port}",
        target=f"{cfg.target.host}:{cfg.target.port}",
    )
    _record_overrides(manifest, overrides)

    plan = build_cluster_plan(cfg, manifest.run_id)
    executor = Executor(ctx.mode)
    copy_data = cfg.init_sync == InitSync.COPY
    save_path = path if ctx.mode is ExecutionMode.EXECUTE else None

    for dbp in plan.databases:
        for w in dbp.warnings:
            log.warning("%s: %s", dbp.db, w)
        _setup_database(ctx, executor, manifest, dbp, copy_data, save_path)

    if ctx.mode is ExecutionMode.EXECUTE:
        manifest.save(path)
        log.info("manifest written to %s", path)
    return manifest


def _setup_database(
    ctx: Context,
    executor: Executor,
    manifest: Manifest,
    dbp: DatabasePlan,
    copy_data: bool,
    save_path=None,
) -> None:
    cfg = ctx.config
    execute = ctx.mode is ExecutionMode.EXECUTE

    # In execute mode we need live connections; otherwise pass None (record only).
    if execute:
        with connect(cfg.source, dbp.db, statement_timeout_ms=cfg.statement_timeout_ms,
                     lock_timeout_ms=cfg.lock_timeout_ms) as sconn, \
             connect(cfg.target, dbp.db) as tconn:
            for spec in dbp.slots:
                _setup_slot(executor, manifest, spec, cfg, copy_data, sconn, tconn, save_path)
    else:
        for spec in dbp.slots:
            _setup_slot(executor, manifest, spec, cfg, copy_data, None, None, None)


def _setup_slot(executor, manifest, spec: SlotSpec, cfg, copy_data, sconn, tconn,
                save_path=None) -> None:
    def _record(state: str) -> None:
        manifest.upsert_slot(
            SlotRecord(
                db=spec.db,
                index=spec.index,
                name=spec.name,
                tables=[t.qualified for t in spec.tables],
                publish=list(spec.publish_ops),
                via_partition_root=spec.via_partition_root,
                init_sync=cfg.init_sync.value,
                state=state,
            )
        )
        if save_path is not None:
            manifest.save(save_path)

    # --- idempotency / conflict (FR-41): adopt only if the object matches the plan ---
    if sconn is not None and catalog.publication_exists(sconn, spec.name):
        details = catalog.publication_details(sconn, spec.name) or {}
        conflicts = reconcile.publication_conflicts(
            spec,
            tables=catalog.publication_tables(sconn, spec.name),
            publish_ops=details.get("publish", set()),
            via_partition_root=details.get("via_partition_root", False),
        )
        if conflicts:
            raise PlanConflict(
                f"publication {spec.name} exists but diverges from the plan: "
                + "; ".join(conflicts),
                hint="resolve the divergence or tear down before re-running (FR-41)",
            )
        log.info("publication %s already matches plan; adopting", spec.name)
    else:
        executor.run(sconn, sqlgen.create_publication(spec))
    # record the publication *before* the subscription so a sub failure is recoverable
    _record("publication_created")

    if sconn is not None and tconn is not None and catalog.subscription_exists(tconn, spec.name):
        details = catalog.subscription_details(tconn, spec.name) or {}
        conflicts = reconcile.subscription_conflicts(
            spec,
            slot_name=details.get("slot_name"),
            publications=details.get("publications"),
            conninfo=details.get("conninfo"),
            expected_conninfo=cfg.source.dsn_for_subscriber(spec.db),
        )
        if conflicts:
            raise PlanConflict(
                f"subscription {spec.name} exists but diverges from the plan: "
                + "; ".join(conflicts),
                hint="a stale/misconfigured subscription with this name exists; "
                "tear it down or reconcile it before re-running (FR-41)",
            )
        log.info("subscription %s already matches plan; adopting", spec.name)
    else:
        # disable_on_error is PG15+; gate by the subscriber (target) version
        doe = True
        if tconn is not None:
            from pgreplkit.core.engine import detect_engine
            doe = detect_engine(tconn).features.disable_on_error
        executor.run(
            tconn,
            sqlgen.create_subscription(spec, cfg.source, copy_data=copy_data, enabled=True,
                                       disable_on_error=doe),
        )

    _record("copying" if copy_data else "streaming")


def summarize(plan_or_manifest: ClusterPlan | Manifest) -> str:
    if isinstance(plan_or_manifest, ClusterPlan):
        return (
            f"{len(plan_or_manifest.databases)} database(s), "
            f"{plan_or_manifest.total_slots} slot(s)"
        )
    return f"{len(plan_or_manifest.slots)} slot(s) in manifest"


# --------------------------------------------------------------------------------------
# Physical-seed provision-mode orchestration (FR-46..52, snapshot-restore / fast-clone)
# --------------------------------------------------------------------------------------

def _require_same_engine(source_kind: EngineKind, init_sync: InitSync) -> EngineKind:
    """Return the engine kind the target MUST be for this physical-seed strategy, and
    refuse cross-engine / unsupported source engines (FR-49)."""
    if init_sync == InitSync.SNAPSHOT_RESTORE:
        if source_kind != EngineKind.RDS:
            raise ConfigError(
                f"snapshot-restore requires an RDS source (source is {source_kind}); "
                "physical seeds are same-engine only (RDS->RDS). Use init-sync `copy` "
                "or AWS DMS for cross-engine.",
            )
        return EngineKind.RDS
    if init_sync == InitSync.AURORA_FAST_CLONE:
        if source_kind != EngineKind.AURORA:
            raise ConfigError(
                f"aurora-fast-clone requires an Aurora source (source is {source_kind}); "
                "physical seeds are same-engine only (Aurora->Aurora). Use init-sync "
                "`copy` or AWS DMS for cross-engine.",
            )
        return EngineKind.AURORA
    raise ConfigError(f"not a physical-seed strategy: {init_sync}")


def _run_setup_physical_seed(
    ctx: Context, *, force: bool, force_correctness: bool = False,
    confirm_provision: bool = False,
) -> Manifest:
    """Orchestrate a physical-seed setup: prepare source (pub+slot) -> snapshot/restore
    target -> capture seed LSN -> wire seed-resume (FR-48..52).
    """
    from pgreplkit.config.models import ExecutionMode as _EM
    from pgreplkit.core.engine import detect_engine
    from pgreplkit.phases.initial_sync import capture_seed_lsn, prepare_source, wire_seed_resume
    from pgreplkit.phases.preflight import run_preflight

    cfg = ctx.config
    if ctx.mode is not _EM.EXECUTE:
        raise ConfigError(
            "physical-seed setup currently supports execute mode only; "
            "use `guide` for a generate-only runbook"
        )

    # 0) detect the SOURCE engine and enforce same-engine strategy compatibility (FR-49)
    entry_db = cfg.source.dbname or "postgres"
    with connect(cfg.source, entry_db, read_only=True) as sc0:
        source_engine = detect_engine(sc0)
    required_kind = _require_same_engine(source_engine.kind, cfg.init_sync)

    # 1) preflight the source (target may not exist yet in provision mode)
    overrides: list[dict] = []
    if cfg.target is not None:
        report = run_preflight(ctx)
        overrides = _gate_preflight(report, force=force, force_correctness=force_correctness)

    # 1b) confirmation gate for billable AWS provisioning (FR-52)
    if cfg.provision_mode == ProvisionMode.PROVISION and not confirm_provision:
        aws = cfg.aws
        target_desc = (
            (aws.target_cluster_id or aws.target_instance_id or "<target>") if aws else "<target>"
        )
        raise ConfirmationRequired(
            "provision mode creates billable AWS resources "
            f"({cfg.init_sync.value}: seed from "
            f"'{aws.source_instance_id if aws else '?'}' -> create '{target_desc}' as "
            f"{aws.target_instance_class if aws else '?'} in {aws.region if aws else '?'})",
            hint="review the target identifier/class/subnet/SG, then re-run with "
            "--confirm-provision (or --yes)",
        )

    project = cfg.project_name()
    path = default_manifest_path(project)
    manifest = Manifest.load(path) or Manifest(
        project=project, run_id=_new_run_id(),
        source=f"{cfg.source.host}:{cfg.source.port}",
        target=(f"{cfg.target.host}:{cfg.target.port}" if cfg.target else "provision"),
    )
    _record_overrides(manifest, overrides)
    plan = build_cluster_plan(cfg, manifest.run_id)
    executor = Executor(ctx.mode)

    # 2) prepare source: CREATE PUBLICATION then slot (pub-before-slot!) for EVERY slot,
    #    across all in-scope databases, BEFORE the snapshot.
    for dbp in plan.databases:
        with connect(cfg.source, dbp.db, statement_timeout_ms=cfg.statement_timeout_ms) as sc:
            for spec in dbp.slots:
                prepare_source(executor, sc, spec)
                manifest.upsert_slot(_slot_record(spec, cfg, "prepared"))
        manifest.save(path)
    log.info("prepared source publications+slots for %d slot(s)", plan.total_slots)

    # 3) provision the target (snapshot-restore) or use the existing restored target
    target = cfg.target
    if cfg.provision_mode == ProvisionMode.PROVISION:
        target = _provision_target(ctx)
        manifest.target = f"{target.host}:{target.port}"
        manifest.save(path)

    if target is None:
        raise ConfigError(
            "physical-seed needs a target: set target (existing mode) or aws.* (provision)"
        )

    # 4) per database: capture seed LSN on the restored target
    # 5) per slot: wire seed-resume (subscription -> origin advance -> enable)
    for dbp in plan.databases:
        with connect(cfg.source, dbp.db) as sc, connect(target, dbp.db) as tc:
            engine = detect_engine(tc)
            if engine.kind != required_kind:
                raise ConfigError(
                    f"physical-seed target engine is {engine.kind} but the source is "
                    f"{source_engine.kind}; physical seeds are same-engine only "
                    f"({required_kind}->{required_kind}). Use init-sync `copy` or DMS."
                )
            seed_lsn = capture_seed_lsn(engine, tc)
            log.info("%s: seed LSN = %s", dbp.db, seed_lsn)
            for spec in dbp.slots:
                wire_seed_resume(executor, sc, tc, spec, cfg.source, seed_lsn)
                rec = _slot_record(spec, cfg, "streaming")
                rec.seed_lsn = seed_lsn
                manifest.upsert_slot(rec)
        manifest.save(path)

    log.info("physical-seed setup complete; manifest at %s", path)
    return manifest


def _slot_record(spec: SlotSpec, cfg, state: str) -> SlotRecord:
    return SlotRecord(
        db=spec.db, index=spec.index, name=spec.name,
        tables=[t.qualified for t in spec.tables],
        publish=list(spec.publish_ops), via_partition_root=spec.via_partition_root,
        init_sync=cfg.init_sync.value, state=state,
    )


def _provision_target(ctx: Context):
    """Provision a same-engine target via boto3 (FR-52): RDS snapshot-restore or
    Aurora fast clone (copy-on-write)."""
    from pgreplkit.aws import rds as awsrds
    from pgreplkit.config.models import Endpoint, InitSync

    cfg = ctx.config
    aws = cfg.aws
    if aws is None or not aws.source_instance_id:
        raise ConfigError("provision mode requires aws.source_instance_id")
    rds = awsrds.RdsClient(profile=aws.profile, region=aws.region).client()

    if cfg.init_sync == InitSync.AURORA_FAST_CLONE:
        if not aws.target_cluster_id:
            raise ConfigError("aurora-fast-clone provision requires aws.target_cluster_id")
        host, port = awsrds.clone_aurora_cluster(
            rds, aws.source_instance_id, aws.target_cluster_id,
            instance_class=aws.target_instance_class,
            subnet_group=aws.subnet_group,
            security_group_ids=aws.security_group_ids or None,
            cluster_parameter_group=aws.parameter_group,
            publicly_accessible=aws.publicly_accessible,
        )
    else:  # snapshot-restore
        if not aws.target_instance_id:
            raise ConfigError("snapshot-restore provision requires aws.target_instance_id")
        snap_id = f"{aws.source_instance_id}-pgrk-{_new_run_id().split('-')[-1]}"
        log.info("snapshot %s -> restore %s (%s)", aws.source_instance_id,
                 aws.target_instance_id, aws.target_instance_class)
        awsrds.create_snapshot(rds, aws.source_instance_id, snap_id)
        inst = awsrds.restore_instance_from_snapshot(
            rds, snap_id, aws.target_instance_id,
            instance_class=aws.target_instance_class,
            subnet_group=aws.subnet_group,
            security_group_ids=aws.security_group_ids or None,
            parameter_group=aws.parameter_group,
            publicly_accessible=aws.publicly_accessible,
        )
        host, port = awsrds.endpoint_of(inst)

    return Endpoint(
        host=host, port=port, user=cfg.source.user,
        password=cfg.source.password, dbname=cfg.source.dbname, sslmode=cfg.source.sslmode,
    )
