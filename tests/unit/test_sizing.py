"""Unit tests for the pure sizing/planning helpers (no DB access)."""

from __future__ import annotations

from pgreplkit.core.catalog import TableSizing
from pgreplkit.core.model import TableRef
from pgreplkit.phases.sizing import build_sizing_rows, human_bytes


def test_human_bytes() -> None:
    assert human_bytes(0) == "0 B"
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MiB"
    assert human_bytes(3 * 1024**3) == "3.0 GiB"


def _sz(schema, name, total, ins=0, upd=0, dele=0, idx=0):
    return TableSizing(
        ref=TableRef(schema, name),
        est_rows=100,
        table_bytes=total // 2,
        index_bytes=total // 2,
        total_bytes=total,
        index_count=idx,
        inserts=ins,
        updates=upd,
        deletes=dele,
    )


def test_build_sizing_rows_rates_and_sort() -> None:
    sizings = [
        _sz("app", "small", 1000, ins=10, upd=0, dele=0, idx=1),
        _sz("app", "big", 1_000_000, ins=200, upd=100, dele=100, idx=3),
    ]
    rows = build_sizing_rows("appdb", sizings, seconds_since_reset=10.0)
    # sorted largest-first by total bytes
    assert [r.qualified for r in rows] == ["app.big", "app.small"]
    big = rows[0]
    assert big.ins_per_sec == 20.0  # 200 / 10
    assert big.upd_per_sec == 10.0
    assert big.del_per_sec == 10.0
    assert big.index_count == 3
    assert big.total_size == human_bytes(1_000_000)


def test_build_sizing_rows_zero_seconds_safe() -> None:
    rows = build_sizing_rows("appdb", [_sz("app", "t", 100, ins=5)], seconds_since_reset=0.0)
    assert rows[0].ins_per_sec == 0.0  # no division by zero


def test_build_sizing_rows_empty() -> None:
    assert build_sizing_rows("appdb", [], 10.0) == []
