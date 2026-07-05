"""Engine detection and capabilities (vanilla / RDS / Aurora) — DESIGN.md §8.

Detection heuristics (read-only):
  - Aurora  : ``aurora_version()`` function exists.
  - RDS     : ``rds_tools`` extension available OR ``rds.*`` GUCs present, and not Aurora.
  - vanilla : otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from pgreplkit.checks.version import Features, features_from_version_num
from pgreplkit.config.models import EngineKind
from pgreplkit.core.connection import fetch_scalar


@dataclass(frozen=True)
class EngineInfo:
    kind: EngineKind
    server_version_num: int
    features: Features

    @property
    def is_managed(self) -> bool:
        return self.kind in (EngineKind.RDS, EngineKind.AURORA)

    @property
    def seed_lsn_sql(self) -> str | None:
        """SQL to capture the physical-seed LSN on this engine (FR-49). None if n/a."""
        if self.kind == EngineKind.AURORA:
            return "SELECT aurora_volume_logical_start_lsn() AS lsn"
        if self.kind == EngineKind.RDS:
            return "SELECT rds_tools.logical_seed_lsn() AS lsn"
        return None


def _exists(conn: psycopg.Connection, sql: str) -> bool:
    try:
        return bool(fetch_scalar(conn, sql))
    except psycopg.Error:
        return False


def detect_engine(conn: psycopg.Connection) -> EngineInfo:
    version_num = int(fetch_scalar(conn, "SHOW server_version_num") or 0)
    features = features_from_version_num(version_num)

    is_aurora = _exists(conn, "SELECT aurora_version()")
    if is_aurora:
        return EngineInfo(EngineKind.AURORA, version_num, features)

    is_rds = _exists(
        conn,
        "SELECT 1 FROM pg_available_extensions WHERE name = 'rds_tools'",
    ) or _exists(
        conn,
        "SELECT 1 FROM pg_settings WHERE name = 'rds.logical_replication'",
    )
    if is_rds:
        return EngineInfo(EngineKind.RDS, version_num, features)

    return EngineInfo(EngineKind.VANILLA, version_num, features)
