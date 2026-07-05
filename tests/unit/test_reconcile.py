"""Unit tests for existing-object conflict detection (FR-41 / H1)."""

from __future__ import annotations

from psycopg.conninfo import make_conninfo

from pgreplkit.core import reconcile
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec

OPS = ("insert", "update", "delete", "truncate")


def _spec(via_root: bool = False) -> SlotSpec:
    return SlotSpec(
        db="appdb",
        index=0,
        name="pgrk_appdb_0",
        tables=(TableRef("public", "orders"), TableRef("public", "order_items")),
        publish_ops=OPS,
        via_partition_root=via_root,
    )


# --- publications ---------------------------------------------------------------------

def test_publication_matches_plan() -> None:
    spec = _spec()
    conflicts = reconcile.publication_conflicts(
        spec,
        tables={TableRef("public", "orders"), TableRef("public", "order_items")},
        publish_ops=set(OPS),
        via_partition_root=False,
    )
    assert conflicts == []


def test_publication_table_set_differs() -> None:
    spec = _spec()
    conflicts = reconcile.publication_conflicts(
        spec,
        tables={TableRef("public", "orders")},  # missing order_items
        publish_ops=set(OPS),
        via_partition_root=False,
    )
    assert any("table set differs" in c for c in conflicts)


def test_publication_publish_ops_differ() -> None:
    spec = _spec()
    conflicts = reconcile.publication_conflicts(
        spec,
        tables=set(spec.tables),
        publish_ops={"insert", "update"},
        via_partition_root=False,
    )
    assert any("publish ops differ" in c for c in conflicts)


def test_publication_via_partition_root_differs() -> None:
    spec = _spec(via_root=True)
    conflicts = reconcile.publication_conflicts(
        spec,
        tables=set(spec.tables),
        publish_ops=set(OPS),
        via_partition_root=False,  # plan wants True
    )
    assert any("publish_via_partition_root differs" in c for c in conflicts)


# --- subscriptions --------------------------------------------------------------------

def _conninfo(host: str = "src.example.com", port: int = 5432, db: str = "appdb") -> str:
    return make_conninfo(host=host, port=port, dbname=db, user="rep", password="s3cr3t")


def test_subscription_matches_plan() -> None:
    spec = _spec()
    conflicts = reconcile.subscription_conflicts(
        spec,
        slot_name=spec.name,
        publications=[spec.name],
        conninfo=_conninfo(),
        expected_conninfo=_conninfo(),
    )
    assert conflicts == []


def test_subscription_slot_name_differs() -> None:
    spec = _spec()
    conflicts = reconcile.subscription_conflicts(
        spec,
        slot_name="some_other_slot",
        publications=[spec.name],
        conninfo=_conninfo(),
        expected_conninfo=_conninfo(),
    )
    assert any("slot_name differs" in c for c in conflicts)


def test_subscription_publication_not_subscribed() -> None:
    spec = _spec()
    conflicts = reconcile.subscription_conflicts(
        spec,
        slot_name=spec.name,
        publications=["unrelated_pub"],
        conninfo=_conninfo(),
        expected_conninfo=_conninfo(),
    )
    assert any("do not include" in c for c in conflicts)


def test_subscription_wrong_source_host() -> None:
    spec = _spec()
    conflicts = reconcile.subscription_conflicts(
        spec,
        slot_name=spec.name,
        publications=[spec.name],
        conninfo=_conninfo(host="WRONG.example.com"),
        expected_conninfo=_conninfo(host="src.example.com"),
    )
    assert any("connection host differs" in c for c in conflicts)


def test_subscription_conninfo_unreadable_is_skipped() -> None:
    # when the catalog does not expose subconninfo (non-privileged), skip that check
    spec = _spec()
    conflicts = reconcile.subscription_conflicts(
        spec,
        slot_name=spec.name,
        publications=[spec.name],
        conninfo=None,
        expected_conninfo=_conninfo(),
    )
    assert conflicts == []
