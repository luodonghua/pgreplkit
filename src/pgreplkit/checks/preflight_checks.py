"""Pure preflight check evaluators (facts in -> CheckResults out). No DB access here,
so each check is unit-testable. Maps to FR-14..29.
"""

from __future__ import annotations

from pgreplkit.checks.results import CheckResult, Level
from pgreplkit.checks.version import Features
from pgreplkit.config.models import EngineKind
from pgreplkit.core.catalog import RelationInfo

# relkind -> human name for messages
_KIND_NAME = {"v": "view", "m": "materialized view", "f": "foreign table"}


def check_replica_identity(relations: list[RelationInfo]) -> list[CheckResult]:
    """FR-14..16: tables need a usable REPLICA IDENTITY for UPDATE/DELETE."""
    out: list[CheckResult] = []
    for rel in relations:
        if not rel.is_ordinary_table:
            continue
        ri = rel.replica_identity
        # d=default(PK), f=full, i=index are safe; n=nothing is not.
        safe = (ri == "d" and rel.has_pk) or ri in ("f", "i")
        if not safe:
            out.append(
                CheckResult(
                    Level.BLOCK,
                    "replica_identity",
                    f"{rel.ref.qualified} has no usable REPLICA IDENTITY "
                    "(no PK/unique index, not FULL); UPDATE/DELETE will not replicate",
                    remediation=(
                        "add a PRIMARY KEY (preferred), or a UNIQUE index + "
                        "REPLICA IDENTITY USING INDEX; or set REPLICA IDENTITY FULL "
                        "(warning: logs full old row -> more WAL/decode cost and a "
                        "sequential scan per changed row on the subscriber); or "
                        "exclude the table from scope"
                    ),
                    subject=rel.ref.qualified,
                )
            )
    return out


def check_relation_kinds(relations: list[RelationInfo]) -> list[CheckResult]:
    """FR-17a: screen relation kinds that cannot be published or won't replicate."""
    out: list[CheckResult] = []
    for rel in relations:
        if rel.relkind in _KIND_NAME:
            out.append(
                CheckResult(
                    Level.BLOCK,
                    "relation_kind",
                    f"{rel.ref.qualified} is a {_KIND_NAME[rel.relkind]}; "
                    "publishing it raises an error in logical replication",
                    remediation="exclude it from scope (it is not a replicable table)",
                    subject=rel.ref.qualified,
                )
            )
        elif rel.persistence == "u":
            out.append(
                CheckResult(
                    Level.WARN,
                    "unlogged_table",
                    f"{rel.ref.qualified} is UNLOGGED; its data is not replicated",
                    remediation="convert to a logged table (SET LOGGED) or exclude it",
                    subject=rel.ref.qualified,
                )
            )
        elif rel.persistence == "t":
            out.append(
                CheckResult(
                    Level.WARN,
                    "temp_table",
                    f"{rel.ref.qualified} is TEMPORARY; it is not replicated",
                    subject=rel.ref.qualified,
                )
            )
    return out


def check_unreplicated_objects(
    db: str, sequence_count: int, large_object_count: int
) -> list[CheckResult]:
    """FR-17: sequences and large objects are not carried by logical replication."""
    out: list[CheckResult] = []
    if sequence_count > 0:
        out.append(
            CheckResult(
                Level.WARN,
                "sequences",
                f"{db}: {sequence_count} sequence(s) are not replicated; "
                "sync them at cutover (pgreplkit sync-sequences)",
                subject=db,
            )
        )
    if large_object_count > 0:
        out.append(
            CheckResult(
                Level.WARN,
                "large_objects",
                f"{db}: {large_object_count} large object(s) are not replicated "
                "and would be absent on the target",
                remediation="migrate large objects separately (e.g. pg_dump)",
                subject=db,
            )
        )
    return out


def check_version(features: Features) -> list[CheckResult]:
    """FR-20/21: enforce minimum supported PostgreSQL version."""
    if not features.supported:
        return [
            CheckResult(
                Level.BLOCK,
                "version",
                f"PostgreSQL major {features.major} is below the minimum supported "
                "version (11)",
                remediation="upgrade to PostgreSQL 11 or newer",
            )
        ]
    return []


def check_source_logical_wal(
    engine: EngineKind, wal_level: str | None, rds_logical: str | None
) -> list[CheckResult]:
    """FR-23/24: logical WAL must be enabled (engine-aware)."""
    if engine in (EngineKind.RDS, EngineKind.AURORA):
        if (rds_logical or "").lower() not in ("on", "1", "true"):
            return [
                CheckResult(
                    Level.BLOCK,
                    "rds_logical_replication",
                    "rds.logical_replication is not enabled on the source",
                    remediation="set rds.logical_replication=1 in the parameter group "
                    "(static — requires a reboot to take effect)",
                )
            ]
        return []
    if (wal_level or "").lower() != "logical":
        return [
            CheckResult(
                Level.BLOCK,
                "wal_level",
                f"source wal_level is '{wal_level}', must be 'logical'",
                remediation="set wal_level=logical and restart the server",
            )
        ]
    return []


def check_target_columns(
    db: str, table_qualified: str, source_cols: list[tuple], target_cols: list[tuple]
) -> list[CheckResult]:
    """FR-18/19: for copy/pre-seeded, each in-scope table must exist on the target with
    compatible columns (name, type, nullability, order)."""
    subject = f"{db}.{table_qualified}"
    if not target_cols:
        return [
            CheckResult(
                Level.BLOCK, "target_table_missing",
                f"{subject} does not exist on the target (logical replication does not "
                "copy DDL)",
                remediation="pre-create the schema: pg_dump --schema-only | psql",
                subject=subject,
            )
        ]
    if source_cols != target_cols:
        return [
            CheckResult(
                Level.BLOCK, "target_columns_mismatch",
                f"{subject} column definitions differ between source and target "
                "(name/type/nullability/order)",
                remediation="align the target table DDL with the source",
                subject=subject,
            )
        ]
    return []


def check_source_params(
    settings: dict,
    slot_demand: int,
    current_slots: int,
    engine_managed: bool,
) -> list[CheckResult]:
    """FR-23: source replication parameters must accommodate the planned slot count.

    Verifies max_replication_slots, max_wal_senders, and max_worker_processes, and
    suggests concrete values when they fall short.
    """
    where = (
        "in the (cluster) parameter group — static, needs a reboot"
        if engine_managed
        else "in postgresql.conf — needs a restart"
    )
    out: list[CheckResult] = []

    slots = int(settings.get("max_replication_slots") or 0)
    senders = int(settings.get("max_wal_senders") or 0)
    workers = int(settings.get("max_worker_processes") or 0)
    free = slots - current_slots
    suggested_slots = current_slots + slot_demand + 2  # planned + small headroom

    if slot_demand > free:
        out.append(
            CheckResult(
                Level.BLOCK, "max_replication_slots",
                f"source max_replication_slots={slots} with {current_slots} in use leaves "
                f"{free} free, but {slot_demand} are needed",
                remediation=f"set max_replication_slots >= {suggested_slots} {where}",
            )
        )
    if slot_demand > senders:
        out.append(
            CheckResult(
                Level.BLOCK, "max_wal_senders",
                f"source max_wal_senders={senders} but {slot_demand} walsenders are needed "
                "(one per active slot)",
                remediation=f"set max_wal_senders >= {slot_demand + 2} {where}",
            )
        )
    if workers and workers < slot_demand + 4:
        out.append(
            CheckResult(
                Level.WARN, "max_worker_processes_source",
                f"source max_worker_processes={workers} is low for {slot_demand} "
                "concurrent logical-decoding backends",
                remediation=f"consider max_worker_processes >= {slot_demand + 8} {where}",
            )
        )
    return out


def check_target_params(
    settings: dict,
    subscription_count: int,
    engine_managed: bool,
) -> list[CheckResult]:
    """FR-26: subscriber replication parameters must run the planned subscriptions.

    Each subscription needs one apply worker; initial table copy adds up to
    max_sync_workers_per_subscription table-sync workers per subscription. All logical
    replication workers are drawn from the max_worker_processes pool.
    """
    where = (
        "in the (cluster) parameter group — static, needs a reboot"
        if engine_managed
        else "in postgresql.conf — needs a restart"
    )
    out: list[CheckResult] = []

    lrw = int(settings.get("max_logical_replication_workers") or 0)
    wp = int(settings.get("max_worker_processes") or 0)
    sync = int(settings.get("max_sync_workers_per_subscription") or 2)
    slots = int(settings.get("max_replication_slots") or 0)

    # peak workers: one apply worker per subscription + table-sync workers during copy
    peak_workers = subscription_count + subscription_count * sync
    suggested_lrw = max(peak_workers, subscription_count + 2)

    if lrw < subscription_count:
        out.append(
            CheckResult(
                Level.BLOCK, "max_logical_replication_workers",
                f"target max_logical_replication_workers={lrw} but {subscription_count} "
                "apply workers are needed (one per subscription)",
                remediation=f"set max_logical_replication_workers >= {suggested_lrw} {where} "
                f"({subscription_count} apply + up to {subscription_count * sync} table-sync)",
            )
        )
    elif lrw < peak_workers:
        out.append(
            CheckResult(
                Level.WARN, "max_logical_replication_workers",
                f"target max_logical_replication_workers={lrw} covers apply but not peak "
                f"initial-copy parallelism ({peak_workers})",
                remediation=f"consider max_logical_replication_workers >= {suggested_lrw} {where}",
            )
        )

    if wp < suggested_lrw + 4:
        out.append(
            CheckResult(
                Level.BLOCK if wp < subscription_count + 1 else Level.WARN,
                "max_worker_processes_target",
                f"target max_worker_processes={wp}; logical replication workers are drawn "
                f"from this pool and need up to {suggested_lrw}",
                remediation=f"set max_worker_processes >= {suggested_lrw + 8} {where}",
            )
        )

    if slots and slots < subscription_count:
        out.append(
            CheckResult(
                Level.WARN, "max_replication_slots_target",
                f"target max_replication_slots={slots} < {subscription_count} subscriptions "
                "(needed if this target later becomes a publisher, e.g. reverse)",
                remediation=f"consider max_replication_slots >= {subscription_count + 2} {where}",
            )
        )
    return out


def check_wal_retention(max_slot_wal_keep_size_mb: int) -> list[CheckResult]:
    """FR-29: two opposite failure modes of max_slot_wal_keep_size."""
    if max_slot_wal_keep_size_mb < 0:
        return [
            CheckResult(
                Level.WARN,
                "wal_retention_unbounded",
                "max_slot_wal_keep_size is -1 (unbounded): an inactive/lagging slot "
                "will retain WAL without limit and can fill the source disk",
                remediation="monitor free storage (and on RDS: CloudWatch "
                "OldestReplicationSlotLag / ReplicationSlotDiskUsage / FreeStorageSpace)",
            )
        ]
    return [
        CheckResult(
            Level.WARN,
            "wal_retention_bounded",
            f"max_slot_wal_keep_size is {max_slot_wal_keep_size_mb}MB: a slot exceeding "
            "this is dropped as 'lost', silently breaking replication (full resync)",
            remediation="size it above expected peak lag / seed-retention window",
        )
    ]


def check_encoding_parity(
    source_encoding: str,
    source_collate: str,
    source_ctype: str,
    target_encoding: str,
    target_collate: str,
    target_ctype: str,
) -> list[CheckResult]:
    """FR-22: encoding/collation mismatch can corrupt ordering/uniqueness."""
    out: list[CheckResult] = []
    if source_encoding != target_encoding:
        out.append(
            CheckResult(
                Level.BLOCK,
                "encoding_mismatch",
                f"encoding differs: source={source_encoding} target={target_encoding}",
                remediation="recreate the target database with matching encoding",
            )
        )
    if (source_collate, source_ctype) != (target_collate, target_ctype):
        out.append(
            CheckResult(
                Level.WARN,
                "collation_mismatch",
                f"collation differs: source={source_collate}/{source_ctype} "
                f"target={target_collate}/{target_ctype}; text ordering/uniqueness "
                "may diverge",
            )
        )
    return out
