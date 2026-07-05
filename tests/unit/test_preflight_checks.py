"""Unit tests for pure preflight check evaluators (FR-14..29)."""

from __future__ import annotations

from pgreplkit.checks import preflight_checks as pc
from pgreplkit.checks.results import Level
from pgreplkit.checks.version import features_from_version_num
from pgreplkit.config.models import EngineKind
from pgreplkit.core.catalog import RelationInfo


def rel(name, relkind="r", persistence="p", replica_identity="d", has_pk=True, has_unique=False):
    return RelationInfo(
        schema="public",
        name=name,
        relkind=relkind,
        persistence=persistence,
        replica_identity=replica_identity,
        has_pk=has_pk,
        has_unique=has_unique,
    )


def _codes(results):
    return {r.code for r in results}


def test_replica_identity_blocks_table_without_pk() -> None:
    rels = [
        rel("good", has_pk=True),
        rel("bad", replica_identity="n", has_pk=False),
        rel("full_ok", replica_identity="f", has_pk=False),
    ]
    results = pc.check_replica_identity(rels)
    assert len(results) == 1
    assert results[0].level == Level.BLOCK
    assert results[0].subject == "public.bad"


def test_relation_kinds_block_view_warn_unlogged() -> None:
    rels = [
        rel("v", relkind="v"),
        rel("m", relkind="m"),
        rel("f", relkind="f"),
        rel("u", persistence="u"),
        rel("t", persistence="t"),
        rel("ok"),
    ]
    results = pc.check_relation_kinds(rels)
    by_level = {r.subject: r.level for r in results}
    assert by_level["public.v"] == Level.BLOCK
    assert by_level["public.m"] == Level.BLOCK
    assert by_level["public.f"] == Level.BLOCK
    assert by_level["public.u"] == Level.WARN
    assert by_level["public.t"] == Level.WARN
    assert "public.ok" not in by_level


def test_source_logical_wal_vanilla_vs_rds() -> None:
    assert pc.check_source_logical_wal(EngineKind.VANILLA, "replica", None)[0].code == "wal_level"
    assert pc.check_source_logical_wal(EngineKind.VANILLA, "logical", None) == []
    assert (
        pc.check_source_logical_wal(EngineKind.RDS, None, "off")[0].code
        == "rds_logical_replication"
    )
    assert pc.check_source_logical_wal(EngineKind.RDS, None, "on") == []


def test_source_params_block_insufficient_slots_and_senders() -> None:
    settings = {"max_replication_slots": "4", "max_wal_senders": "2",
                "max_worker_processes": "8"}
    results = pc.check_source_params(settings, slot_demand=5, current_slots=2,
                                     engine_managed=True)
    codes = {r.code: r for r in results}
    assert codes["max_replication_slots"].level == Level.BLOCK
    assert codes["max_wal_senders"].level == Level.BLOCK
    # suggestion is concrete (current 2 + demand 5 + 2 = 9)
    assert "max_replication_slots >= 9" in codes["max_replication_slots"].remediation


def test_source_params_ok() -> None:
    settings = {"max_replication_slots": "20", "max_wal_senders": "20",
                "max_worker_processes": "30"}
    assert pc.check_source_params(settings, slot_demand=4, current_slots=0,
                                  engine_managed=False) == []


def test_target_params_block_workers() -> None:
    # 6 subscriptions but only 4 apply workers, 8 worker_processes
    settings = {"max_logical_replication_workers": "4", "max_worker_processes": "8",
                "max_sync_workers_per_subscription": "2", "max_replication_slots": "10"}
    results = pc.check_target_params(settings, subscription_count=6, engine_managed=True)
    codes = {r.code: r for r in results}
    assert codes["max_logical_replication_workers"].level == Level.BLOCK
    rem = codes["max_logical_replication_workers"].remediation
    assert "max_logical_replication_workers >=" in rem


def test_target_params_warn_no_sync_headroom() -> None:
    # apply covered (>=count) but not peak initial-copy parallelism
    settings = {"max_logical_replication_workers": "6", "max_worker_processes": "40",
                "max_sync_workers_per_subscription": "2", "max_replication_slots": "10"}
    results = pc.check_target_params(settings, subscription_count=6, engine_managed=True)
    lrw = next(r for r in results if r.code == "max_logical_replication_workers")
    assert lrw.level == Level.WARN


def test_target_params_ok() -> None:
    settings = {"max_logical_replication_workers": "20", "max_worker_processes": "40",
                "max_sync_workers_per_subscription": "2", "max_replication_slots": "20"}
    assert pc.check_target_params(settings, subscription_count=4, engine_managed=False) == []


def test_wal_retention_dual_modes() -> None:
    assert pc.check_wal_retention(-1)[0].code == "wal_retention_unbounded"
    assert pc.check_wal_retention(1024)[0].code == "wal_retention_bounded"


def test_encoding_parity() -> None:
    block = pc.check_encoding_parity("UTF8", "en_US.utf8", "en_US.utf8",
                                     "LATIN1", "en_US.utf8", "en_US.utf8")
    assert any(r.code == "encoding_mismatch" and r.level == Level.BLOCK for r in block)
    warn = pc.check_encoding_parity("UTF8", "C", "C", "UTF8", "en_US.utf8", "en_US.utf8")
    assert any(r.code == "collation_mismatch" and r.level == Level.WARN for r in warn)
    assert pc.check_encoding_parity("UTF8", "C", "C", "UTF8", "C", "C") == []


def test_version_block_below_min() -> None:
    assert pc.check_version(features_from_version_num(100023))[0].code == "version"
    assert pc.check_version(features_from_version_num(160004)) == []


def test_target_columns_missing_and_mismatch() -> None:
    src = [("id", "integer", "NO"), ("v", "text", "YES")]
    # missing on target
    r = pc.check_target_columns("appdb", "public.t", src, [])
    assert r and r[0].code == "target_table_missing" and r[0].level == Level.BLOCK
    # column mismatch
    r = pc.check_target_columns("appdb", "public.t", src, [("id", "bigint", "NO")])
    assert r and r[0].code == "target_columns_mismatch" and r[0].level == Level.BLOCK
    # identical -> ok
    assert pc.check_target_columns("appdb", "public.t", src, src) == []

    results = pc.check_unreplicated_objects("appdb", sequence_count=2, large_object_count=5)
    assert _codes(results) == {"sequences", "large_objects"}
    assert all(r.level == Level.WARN for r in results)
