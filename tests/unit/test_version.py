"""Unit tests for version feature gating (FR-20, FR-21, FR-28)."""

from __future__ import annotations

from pgreplkit.checks.version import (
    features_from_version_num,
    major_from_version_num,
)


def test_major_from_version_num() -> None:
    assert major_from_version_num(160004) == 16
    assert major_from_version_num(150007) == 15
    assert major_from_version_num(110021) == 11


def test_pg16_has_all_features() -> None:
    f = features_from_version_num(160004)
    assert f.supported
    assert f.truncate_replication
    assert f.streaming
    assert f.two_phase
    assert f.row_filter
    assert f.parallel_apply
    assert f.origin_none
    assert not f.subscription_needs_superuser


def test_pg14_no_parallel_apply_or_row_filter() -> None:
    f = features_from_version_num(140010)
    assert f.streaming
    assert not f.row_filter
    assert not f.parallel_apply
    assert not f.origin_none
    assert f.subscription_needs_superuser  # < 16
    assert not f.disable_on_error  # PG15+ only


def test_pg12_below_min_still_reports_flags() -> None:
    f = features_from_version_num(120015)
    assert f.truncate_replication  # 11+
    assert not f.streaming
    assert f.subscription_needs_superuser


def test_pg10_unsupported() -> None:
    f = features_from_version_num(100023)
    assert not f.supported
    assert not f.truncate_replication
