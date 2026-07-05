"""Configuration models (pydantic v2) and enums.

Maps to REQUIREMENTS.md FR-1..3 (connectivity), FR-4..9 (scope), FR-10..13 (slots),
FR-46/FR-52 (init-sync / provisioning), and the execution modes.
"""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator


class StrEnum(str, Enum):
    """str-Enum compatible with Python 3.10 (stdlib StrEnum is 3.11+)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class EngineKind(StrEnum):
    VANILLA = "vanilla"
    RDS = "rds"
    AURORA = "aurora"


class SlotStrategy(StrEnum):
    SINGLE = "single"
    PER_SCHEMA = "per-schema"
    BALANCED = "balanced"
    MANUAL = "manual"


class InitSync(StrEnum):
    COPY = "copy"
    SNAPSHOT_RESTORE = "snapshot-restore"
    AURORA_FAST_CLONE = "aurora-fast-clone"
    NONE = "none"


class ProvisionMode(StrEnum):
    EXISTING = "existing"
    PROVISION = "provision"


class ExecutionMode(StrEnum):
    EXECUTE = "execute"
    DRY_RUN = "dry-run"
    GENERATE_ONLY = "generate-only"


class ValidateDepth(StrEnum):
    NONE = "none"
    SAMPLED = "sampled"
    FULL = "full"


class Endpoint(BaseModel):
    """A PostgreSQL endpoint (FR-1..3). Passwords are never logged (FR-2)."""

    host: str
    port: int = 5432
    user: str
    password: SecretStr | None = None
    dbname: str | None = None  # maintenance/entry db for discovery
    sslmode: str | None = None
    secret_ref: str | None = None  # e.g. AWS Secrets Manager ARN
    # How the *subscriber* reaches this endpoint (CREATE SUBSCRIPTION CONNECTION).
    # May differ from host/port (which is how the tool itself connects) — FR-27.
    advertised_host: str | None = None
    advertised_port: int | None = None
    # Client DNS TTL (seconds) for the tool's own connections. None -> auto:
    # 1s for managed RDS/Aurora endpoints, 5s otherwise.
    dns_ttl: int | None = None

    def dsn(self, dbname: str | None = None) -> str:
        """Build a libpq DSN for the tool's own connection (properly escaped)."""
        return self._dsn(self.host, self.port, dbname)

    def dsn_for_subscriber(self, dbname: str | None = None) -> str:
        """DSN a subscriber uses to reach this endpoint (uses advertised host/port)."""
        return self._dsn(
            self.advertised_host or self.host,
            self.advertised_port or self.port,
            dbname,
        )

    def conn_params(self, host: str, port: int, dbname: str | None) -> dict:
        params: dict = {"host": host, "port": port, "user": self.user}
        db = dbname or self.dbname
        if db:
            params["dbname"] = db
        if self.password is not None:
            params["password"] = self.password.get_secret_value()
        if self.sslmode:
            params["sslmode"] = self.sslmode
        return params

    def _dsn(self, host: str, port: int, dbname: str | None) -> str:
        # make_conninfo performs proper libpq escaping (spaces/quotes/backslashes in
        # passwords, etc.) — hand-built key=value joins do not (REVIEW H1).
        from psycopg.conninfo import make_conninfo

        return make_conninfo(**self.conn_params(host, port, dbname))


class Scope(BaseModel):
    """Database/schema/table selection and skip rules (FR-4..9)."""

    databases: list[str] | None = None  # explicit selection overrides discovery
    include_schemas: list[str] = Field(default_factory=lambda: ["*"])
    exclude_schemas: list[str] = Field(default_factory=list)
    include_tables: list[str] = Field(default_factory=lambda: ["*"])
    exclude_tables: list[str] = Field(default_factory=list)
    skip_system_dbs: bool = True   # template0/1, rdsadmin, datallowconn=false (FR-5)
    skip_empty_dbs: bool = True    # FR-6


# System/maintenance databases skipped by default (FR-5, §8).
SYSTEM_DATABASES: frozenset[str] = frozenset({"template0", "template1", "rdsadmin"})


ALLOWED_PUBLISH_OPS: frozenset[str] = frozenset({"insert", "update", "delete", "truncate"})


class SlotConfig(BaseModel):
    """Slot-allocation strategy and bounds (FR-10..13)."""

    strategy: SlotStrategy = SlotStrategy.BALANCED  # default (FR-10)
    n: int = 4                       # balanced slot count
    max_slots: int = 8               # decode-cost cap (FR-13)
    spread_partitions: bool = False  # FR-11 modifier
    slot_map: Path | None = None     # required for MANUAL (FR-12)
    weight_window: timedelta | None = None  # optional non-default sampling window
    publish: list[str] = Field(       # published operations (FR-37)
        default_factory=lambda: ["insert", "update", "delete", "truncate"]
    )

    @model_validator(mode="after")
    def _check(self) -> SlotConfig:
        if self.strategy == SlotStrategy.MANUAL and self.slot_map is None:
            raise ValueError("slot_map is required when strategy=manual (FR-12)")
        if self.n < 1:
            raise ValueError("n must be >= 1")
        if self.max_slots < 1:
            raise ValueError("max_slots must be >= 1")
        if self.strategy == SlotStrategy.BALANCED and self.n > self.max_slots:
            raise ValueError(
                f"requested n={self.n} exceeds max_slots={self.max_slots} "
                "(decode-cost cap, FR-13)"
            )
        if not self.publish:
            raise ValueError("publish must list at least one operation (FR-37)")
        bad = [op for op in self.publish if op not in ALLOWED_PUBLISH_OPS]
        if bad:
            raise ValueError(
                f"invalid publish operation(s) {bad}; allowed: "
                f"{sorted(ALLOWED_PUBLISH_OPS)} (FR-37)"
            )
        return self


class AwsProvisionConfig(BaseModel):
    """AWS settings for `provision` mode and physical-seed strategies (FR-52/52a)."""

    profile: str | None = None
    region: str = "us-east-1"
    source_instance_id: str | None = None   # RDS instance / Aurora cluster to seed from
    target_instance_id: str | None = None   # instance id to create (snapshot-restore)
    target_cluster_id: str | None = None     # cluster id to create (aurora fast clone)
    target_instance_class: str = "db.t3.medium"
    security_group_ids: list[str] = Field(default_factory=list)
    subnet_group: str | None = None
    parameter_group: str | None = None
    publicly_accessible: bool = True


class Config(BaseModel):
    """Top-level run configuration."""

    source: Endpoint
    target: Endpoint | None = None
    scope: Scope = Field(default_factory=Scope)
    slots: SlotConfig = Field(default_factory=SlotConfig)
    init_sync: InitSync = InitSync.COPY
    provision_mode: ProvisionMode = ProvisionMode.EXISTING
    aws: AwsProvisionConfig | None = None
    project: str | None = None        # manifest/project name (FR-74)
    object_prefix: str = "pgrk"       # naming convention prefix (FR-43)
    lag_threshold_bytes: int = 0      # ready-gate threshold (FR-64)
    statement_timeout_ms: int = 0     # 0 = server default (FR-39, NFR-10)
    lock_timeout_ms: int = 0
    concurrency: int = 4              # NFR-4

    def project_name(self) -> str:
        if self.project:
            return self.project
        return f"{self.source.host}_{self.source.port}".replace(".", "_")
