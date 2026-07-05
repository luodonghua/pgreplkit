"""Unit tests for the pure ready-gate evaluator (FR-64)."""

from __future__ import annotations

from pgreplkit.phases.ready import evaluate_ready
from pgreplkit.phases.status import SlotStatus


def slot(**kw) -> SlotStatus:
    base = dict(
        db="appdb",
        name="pgrk_x_appdb_0",
        sub_enabled=True,
        tables_total=2,
        tables_ready=2,
        slot_active=True,
        wal_status="reserved",
        lag_bytes=0,
    )
    base.update(kw)
    return SlotStatus(**base)


def test_ready_passes_when_all_good() -> None:
    assert evaluate_ready([slot()], 0).passed


def test_not_ready_when_no_slots() -> None:
    r = evaluate_ready([], 0)
    assert not r.passed
    assert any("no slots" in x for x in r.reasons)


def test_not_ready_when_sync_incomplete() -> None:
    r = evaluate_ready([slot(tables_ready=1, tables_total=2)], 0)
    assert not r.passed
    assert any("initial sync incomplete" in x for x in r.reasons)


def test_not_ready_when_slot_lost() -> None:
    r = evaluate_ready([slot(wal_status="lost")], 0)
    assert not r.passed
    assert any("LOST" in x for x in r.reasons)


def test_not_ready_when_inactive() -> None:
    assert not evaluate_ready([slot(slot_active=False)], 0).passed


def test_not_ready_when_slot_missing() -> None:
    # missing source slot -> all-None health must be a hard block, not a silent pass (H3)
    r = evaluate_ready([slot(slot_active=None, lag_bytes=None, wal_status=None)], 0)
    assert not r.passed
    assert any("slot not found" in x for x in r.reasons)


def test_lag_threshold() -> None:
    assert evaluate_ready([slot(lag_bytes=100)], 200).passed
    assert not evaluate_ready([slot(lag_bytes=300)], 200).passed
