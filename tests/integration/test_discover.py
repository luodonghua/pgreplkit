"""Integration tests for discover (FR-4..9). Require a live PostgreSQL."""

from __future__ import annotations

import uuid

import pytest

from pgreplkit.config.models import Endpoint, Scope
from pgreplkit.core.connection import connect
from pgreplkit.core.topology import discover_topology

pytestmark = pytest.mark.integration


@pytest.fixture()
def seeded_dbs(source_endpoint: Endpoint):
    """Create a with-tables db and an empty db; drop them after."""
    suffix = uuid.uuid4().hex[:8]
    full = f"pgrk_it_full_{suffix}"
    empty = f"pgrk_it_empty_{suffix}"
    with connect(source_endpoint, "postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{full}"')
        conn.execute(f'CREATE DATABASE "{empty}"')
    with connect(source_endpoint, full, autocommit=True) as conn:
        conn.execute("CREATE TABLE customers (id int PRIMARY KEY, name text)")
        conn.execute(
            "CREATE TABLE orders (id int PRIMARY KEY, "
            "customer_id int REFERENCES customers(id))"
        )
    try:
        yield full, empty
    finally:
        with connect(source_endpoint, "postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{full}" WITH (FORCE)')
            conn.execute(f'DROP DATABASE IF EXISTS "{empty}" WITH (FORCE)')


def test_discover_classifies_scope(source_endpoint: Endpoint, seeded_dbs) -> None:
    full, empty = seeded_dbs
    report = discover_topology(source_endpoint, Scope())
    by_name = {d.name: d for d in report.databases}

    # with-tables db is in scope with 2 tables
    assert full in by_name
    assert by_name[full].included is True
    assert by_name[full].table_count == 2

    # empty db is skipped (FR-6)
    assert empty in by_name
    assert by_name[empty].included is False
    assert "no in-scope user tables" in (by_name[empty].reason or "")

    # system databases skipped (FR-5)
    assert by_name["template0"].included is False
    assert by_name["template1"].included is False
