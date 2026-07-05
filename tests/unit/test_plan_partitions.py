"""Wiring tests for partitioned-table handling in build_cluster_plan (FR-11).

The pure planner is covered in test_slotplan; here we verify the catalog-facing glue:
child partitions are not published standalone, the partitioned root is one unit with
publish_via_partition_root=true when NOT spreading, and leaves are spread with
publish_via_partition_root=false when --spread-partitions is set.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from pgreplkit.config.models import Config, Endpoint, SlotConfig, SlotStrategy
from pgreplkit.core import plan as plan_mod
from pgreplkit.core.catalog import RelationInfo
from pgreplkit.core.model import TableRef

RELATIONS = [
    RelationInfo("public", "orders", "r", "p", "d", True, False, is_partition=False),
    RelationInfo("public", "events", "p", "p", "d", True, False, is_partition=False),
    RelationInfo("public", "events_2024", "r", "p", "d", True, False, is_partition=True),
    RelationInfo("public", "events_2025", "r", "p", "d", True, False, is_partition=True),
]
WEIGHTS = {
    TableRef("public", "orders"): 5.0,
    TableRef("public", "events_2024"): 10.0,
    TableRef("public", "events_2025"): 20.0,
}
PARTMAP = {
    TableRef("public", "events"): [
        TableRef("public", "events_2024"),
        TableRef("public", "events_2025"),
    ]
}


@pytest.fixture
def patched(monkeypatch):
    @contextmanager
    def _fake_connect(*a, **k):
        yield SimpleNamespace()

    monkeypatch.setattr(plan_mod, "connect", _fake_connect)
    monkeypatch.setattr(
        plan_mod, "discover_topology",
        lambda *a, **k: SimpleNamespace(in_scope=[SimpleNamespace(name="appdb")]),
    )
    monkeypatch.setattr(plan_mod.catalog, "list_relations", lambda c: RELATIONS)
    monkeypatch.setattr(plan_mod.catalog, "table_weights", lambda c: WEIGHTS)
    monkeypatch.setattr(plan_mod.catalog, "fk_edges", lambda c: [])
    monkeypatch.setattr(plan_mod.catalog, "partition_map", lambda c: PARTMAP)


def _cfg(spread: bool) -> Config:
    return Config(
        source=Endpoint(host="h", user="u", dbname="appdb"),
        target=Endpoint(host="t", user="u", dbname="appdb"),
        slots=SlotConfig(strategy=SlotStrategy.SINGLE, spread_partitions=spread),
    )


def test_non_spread_publishes_partition_root_as_unit(patched) -> None:
    plan = plan_mod.build_cluster_plan(_cfg(spread=False), "run1")
    slots = plan.databases[0].slots
    all_tables = {t.qualified for s in slots for t in s.tables}
    # leaves are NOT standalone; the root represents them
    assert "public.events" in all_tables
    assert "public.events_2024" not in all_tables
    assert "public.events_2025" not in all_tables
    assert "public.orders" in all_tables
    # the slot holding the partitioned root publishes via the root
    root_slot = next(s for s in slots if any(t.qualified == "public.events" for t in s.tables))
    assert root_slot.via_partition_root is True


def test_spread_distributes_leaves(patched) -> None:
    cfg = _cfg(spread=True)
    cfg.slots = SlotConfig(strategy=SlotStrategy.BALANCED, n=2, max_slots=8,
                           spread_partitions=True)
    plan = plan_mod.build_cluster_plan(cfg, "run1")
    slots = plan.databases[0].slots
    all_tables = {t.qualified for s in slots for t in s.tables}
    assert "public.events" not in all_tables            # root replaced by leaves
    assert "public.events_2024" in all_tables
    assert "public.events_2025" in all_tables
    # spreading publishes leaves by their own identity (not via root)
    for s in slots:
        assert s.via_partition_root is False
