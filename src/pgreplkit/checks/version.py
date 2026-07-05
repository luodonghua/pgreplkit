"""PostgreSQL logical-replication feature gating by server version (FR-20, FR-21).

Feature availability (major version introduced):
  - TRUNCATE replication .............. 11
  - streaming in-progress txns ........ 14
  - two-phase commit .................. 15  (14 had prepare infra; usable via subs in 15)
  - row filters ....................... 15
  - column lists ...................... 15
  - FOR TABLES IN SCHEMA publications .. 15
  - parallel apply .................... 16
  - origin = none (loop avoidance) .... 16
  - pg_create_subscription role ....... 16 (below 16, CREATE SUBSCRIPTION needs superuser)
"""

from __future__ import annotations

from dataclasses import dataclass

# Minimum PostgreSQL major version pgreplkit supports at all.
MIN_SUPPORTED_MAJOR = 11


@dataclass(frozen=True)
class Features:
    major: int

    @property
    def truncate_replication(self) -> bool:
        return self.major >= 11

    @property
    def streaming(self) -> bool:
        return self.major >= 14

    @property
    def two_phase(self) -> bool:
        return self.major >= 15

    @property
    def disable_on_error(self) -> bool:
        """CREATE SUBSCRIPTION ... WITH (disable_on_error) is PG15+."""
        return self.major >= 15

    @property
    def row_filter(self) -> bool:
        return self.major >= 15

    @property
    def column_list(self) -> bool:
        return self.major >= 15

    @property
    def schema_publication(self) -> bool:
        return self.major >= 15

    @property
    def parallel_apply(self) -> bool:
        return self.major >= 16

    @property
    def origin_none(self) -> bool:
        """Loop-avoidance for bidirectional/reverse setups (FR-72)."""
        return self.major >= 16

    @property
    def subscription_needs_superuser(self) -> bool:
        """True on self-managed PG < 16 (no pg_create_subscription) (FR-28, NFR-7)."""
        return self.major < 16

    @property
    def supported(self) -> bool:
        return self.major >= MIN_SUPPORTED_MAJOR


def major_from_version_num(server_version_num: int) -> int:
    """Convert ``server_version_num`` (e.g. 160004) to a major version (e.g. 16).

    PG10+ uses the two-component scheme where major = num // 10000.
    """
    return server_version_num // 10000


def features_from_version_num(server_version_num: int) -> Features:
    return Features(major=major_from_version_num(server_version_num))
