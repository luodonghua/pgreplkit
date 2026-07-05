"""Unit tests for the pure slot-planning engine (FR-10..13)."""

from __future__ import annotations

import pytest

from pgreplkit.config.models import SlotConfig, SlotStrategy
from pgreplkit.core.model import TableInput, TableRef
from pgreplkit.core.slotplan import (
    fk_affinity_groups,
    lpt_pack,
    plan_database_slots,
)
from pgreplkit.errors import ConfigError, SlotCapExceeded


def t(name: str, weight: float = 0.0, schema: str = "public") -> TableInput:
    return TableInput(ref=TableRef(schema, name), weight=weight)


def ref(name: str, schema: str = "public") -> TableRef:
    return TableRef(schema, name)


# --- helpers / primitives -------------------------------------------------------------

def test_tableref_parse_and_quote() -> None:
    assert TableRef.parse("orders") == TableRef("public", "orders")
    assert TableRef.parse("sales.orders") == TableRef("sales", "orders")
    assert TableRef("public", 'weird"name').quoted == '"public"."weird""name"'


def test_fk_affinity_unions_connected_tables() -> None:
    tables = [t("orders", 10), t("order_items", 5), t("customers", 3), t("audit", 1)]
    edges = [(ref("order_items"), ref("orders")), (ref("orders"), ref("customers"))]
    groups = fk_affinity_groups(tables, edges)
    # orders+order_items+customers form one group (weight 18); audit alone (1)
    sizes = sorted(len(g.tables) for g in groups)
    assert sizes == [1, 3]
    big = max(groups, key=lambda g: g.weight)
    assert big.weight == 18
    assert set(big.tables) == {ref("orders"), ref("order_items"), ref("customers")}


def test_lpt_pack_balances_load() -> None:
    tables = [t(f"tbl{i}", w) for i, w in enumerate([8, 6, 4, 2])]
    groups = fk_affinity_groups(tables, [])
    bins = lpt_pack(groups, 2)
    loads = sorted(sum(g.weight for g in b) for b in bins)
    # 8,6,4,2 -> LPT gives bins {8,2}=10 and {6,4}=10
    assert loads == [10, 10]


# --- strategies -----------------------------------------------------------------------

def test_single_puts_everything_in_one_slot() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.SINGLE)
    plan = plan_database_slots("appdb", [t("a", 1), t("b", 2)], [], cfg)
    assert plan.slot_count == 1
    assert set(plan.slots[0].tables) == {ref("a"), ref("b")}


def test_per_schema_one_slot_per_schema() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.PER_SCHEMA)
    tables = [t("a", 1, schema="public"), t("b", 1, schema="sales"), t("c", 1, schema="sales")]
    plan = plan_database_slots("appdb", tables, [], cfg)
    assert plan.slot_count == 2
    schemas = {tbl.schema for s in plan.slots for tbl in s.tables}
    assert schemas == {"public", "sales"}


def test_balanced_respects_n_and_keeps_fk_together() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.BALANCED, n=2, max_slots=8)
    tables = [t("orders", 10), t("order_items", 5), t("logs", 20)]
    edges = [(ref("order_items"), ref("orders"))]
    plan = plan_database_slots("appdb", tables, edges, cfg)
    assert plan.slot_count == 2
    # orders + order_items must be in the same slot (FK affinity)
    for s in plan.slots:
        has_orders = ref("orders") in s.tables
        has_items = ref("order_items") in s.tables
        assert has_orders == has_items
    # no cross-slot FK warning expected
    assert not any("cross-slot FK" in w for w in plan.warnings)


def test_balanced_caps_bins_to_group_count() -> None:
    # Only 1 FK-group but n=4 -> should not create 4 empty bins
    cfg = SlotConfig(strategy=SlotStrategy.BALANCED, n=4, max_slots=8)
    tables = [t("a", 1), t("b", 1)]
    edges = [(ref("a"), ref("b"))]
    plan = plan_database_slots("appdb", tables, edges, cfg)
    assert plan.slot_count == 1


def test_balanced_exceeding_cap_raises() -> None:
    # SlotConfig validator blocks n>max_slots at construction
    with pytest.raises(ValueError):
        SlotConfig(strategy=SlotStrategy.BALANCED, n=10, max_slots=4)


def test_decode_cost_cap_enforced_in_planner() -> None:
    # per-schema producing more slots than max_slots should raise
    cfg = SlotConfig(strategy=SlotStrategy.PER_SCHEMA, max_slots=2)
    tables = [t("a", schema="s1"), t("b", schema="s2"), t("c", schema="s3")]
    with pytest.raises(SlotCapExceeded):
        plan_database_slots("appdb", tables, [], cfg)


def test_partition_spreading_expands_parent() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.BALANCED, n=3, max_slots=8, spread_partitions=True)
    parent = TableInput(
        ref=ref("events"),
        weight=30,
        partitions=(ref("events_2024"), ref("events_2025"), ref("events_2026")),
    )
    plan = plan_database_slots("appdb", [parent], [], cfg)
    all_tables = {tbl for s in plan.slots for tbl in s.tables}
    assert ref("events") not in all_tables  # parent replaced
    assert all_tables == {ref("events_2024"), ref("events_2025"), ref("events_2026")}


def test_partition_spreading_uses_real_leaf_weights() -> None:
    # one hot leaf should be isolated from two cold leaves by LPT packing
    cfg = SlotConfig(strategy=SlotStrategy.BALANCED, n=2, max_slots=8, spread_partitions=True)
    parent = TableInput(
        ref=ref("events"),
        weight=100,
        partitions=(ref("e_hot"), ref("e_cold1"), ref("e_cold2")),
        partition_weights=(100.0, 1.0, 1.0),
    )
    plan = plan_database_slots("appdb", [parent], [], cfg)
    # the heavy leaf lands alone; the two light leaves share the other slot
    slot_of = {t: s.index for s in plan.slots for t in s.tables}
    assert slot_of[ref("e_cold1")] == slot_of[ref("e_cold2")]
    assert slot_of[ref("e_hot")] != slot_of[ref("e_cold1")]


def test_partition_not_spread_keeps_parent_as_unit() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.BALANCED, n=3, max_slots=8, spread_partitions=False)
    parent = TableInput(
        ref=ref("events"),
        weight=30,
        partitions=(ref("events_2024"), ref("events_2025")),
        partition_weights=(15.0, 15.0),
    )
    plan = plan_database_slots("appdb", [parent], [], cfg)
    all_tables = {tbl for s in plan.slots for tbl in s.tables}
    assert all_tables == {ref("events")}  # parent kept whole; leaves not exposed


# --- manual --------------------------------------------------------------------------

def test_manual_glob_matches_schema_wildcard() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [
        t("orders"),
        TableInput(ref=ref("a1", schema="audit"), weight=0),
        TableInput(ref=ref("a2", schema="audit"), weight=0),
    ]
    mapping = {"hot": ["public.orders"], "audit": ["audit.*"]}
    plan = plan_database_slots("appdb", tables, [], cfg, manual_map=mapping)
    audit_slot = next(s for s in plan.slots if s.index == 1)
    assert set(audit_slot.tables) == {ref("a1", "audit"), ref("a2", "audit")}


def test_manual_explicit_beats_glob() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [t("orders"), t("logs")]
    mapping = {"exact": ["public.orders"], "rest": ["public.*"]}
    plan = plan_database_slots("appdb", tables, [], cfg, manual_map=mapping)
    exact = next(s for s in plan.slots if s.index == 0)
    rest = next(s for s in plan.slots if s.index == 1)
    assert ref("orders") in exact.tables and ref("orders") not in rest.tables
    assert ref("logs") in rest.tables


def test_manual_glob_ambiguous_across_slots_warns() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [t("orders")]
    mapping = {"a": ["public.*"], "b": ["*.orders"]}
    plan = plan_database_slots("appdb", tables, [], cfg, manual_map=mapping)
    assert any("ambiguous" in w for w in plan.warnings)


# --- manual ---------------------------------------------------------------------------

def test_manual_requires_full_coverage_or_catchall() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [t("orders"), t("order_items"), t("audit")]
    mapping = {"hot": ["public.orders", "public.order_items"]}  # audit unassigned
    with pytest.raises(ConfigError):
        plan_database_slots("appdb", tables, [], cfg, manual_map=mapping)


def test_manual_catchall_captures_rest() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [t("orders"), t("order_items"), t("audit")]
    mapping = {"hot": ["public.orders", "public.order_items"], "rest": ["*"]}
    plan = plan_database_slots("appdb", tables, [], cfg, manual_map=mapping)
    assert plan.slot_count == 2
    rest = next(s for s in plan.slots if s.index == 1)
    assert ref("audit") in rest.tables


def test_manual_flags_cross_slot_fk() -> None:
    cfg = SlotConfig(strategy=SlotStrategy.MANUAL, slot_map="dummy.yml")
    tables = [t("orders"), t("order_items")]
    mapping = {"a": ["public.orders"], "b": ["public.order_items"]}
    edges = [(ref("order_items"), ref("orders"))]
    plan = plan_database_slots("appdb", tables, edges, cfg, manual_map=mapping)
    assert any("cross-slot FK" in w for w in plan.warnings)
