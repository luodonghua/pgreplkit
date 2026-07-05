"""Integration test for FR-11 partition spreading against a live PostgreSQL pair.

Verifies both modes end-to-end with a RANGE-partitioned table:
  - non-spread : the partitioned root is published as one unit
                 (publish_via_partition_root=true) and all rows replicate;
  - spread     : leaf partitions are distributed across slots by weight
                 (publish_via_partition_root=false) and all rows replicate.

Requires source AND target live PostgreSQL with logical WAL and target->source
reachability (advertised host/port). Skips if env not set.
"""

from __future__ import annotations

import time
import uuid

import pytest

from pgreplkit.config.models import Config, Endpoint, Scope, SlotConfig, SlotStrategy
from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect, fetch_all, fetch_scalar
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import build_cluster_plan
from pgreplkit.phases.setup import run_setup
from pgreplkit.phases.teardown import run_teardown

pytestmark = pytest.mark.integration

_DDL = """
CREATE TABLE events (
    id bigserial, yr int NOT NULL, payload text, PRIMARY KEY (id, yr)
) PARTITION BY RANGE (yr);
CREATE TABLE events_2023 PARTITION OF events FOR VALUES FROM (2023) TO (2024);
CREATE TABLE events_2024 PARTITION OF events FOR VALUES FROM (2024) TO (2025);
CREATE TABLE events_2025 PARTITION OF events FOR VALUES FROM (2025) TO (2026);
CREATE TABLE orders (id serial PRIMARY KEY, v text);
"""
_SEED = """
INSERT INTO events (yr, payload) SELECT 2023, 'a'||g FROM generate_series(1,10) g;
INSERT INTO events (yr, payload) SELECT 2024, 'b'||g FROM generate_series(1,40) g;
INSERT INTO events (yr, payload) SELECT 2025, 'c'||g FROM generate_series(1,100) g;
INSERT INTO orders (v) SELECT 'o'||g FROM generate_series(1,5) g;
"""
_EXPECTED = {"events": 150, "events_2023": 10, "events_2024": 40, "events_2025": 100,
             "orders": 5}


@pytest.fixture()
def partitioned_db(source_endpoint: Endpoint, target_endpoint: Endpoint, tmp_path, monkeypatch):
    monkeypatch.setenv("PGREPLKIT_HOME", str(tmp_path))
    name = f"pgrk_it_part_{uuid.uuid4().hex[:8]}"
    for ep in (source_endpoint, target_endpoint):
        with connect(ep, "postgres", autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
        with connect(ep, name, autocommit=True) as c:
            c.execute(_DDL)
    with connect(source_endpoint, name, autocommit=True) as c:
        c.execute(_SEED)
    try:
        yield name
    finally:
        for ep in (source_endpoint, target_endpoint):
            with connect(ep, "postgres", autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _counts(ep: Endpoint, name: str) -> dict:
    with connect(ep, name, read_only=True) as c:
        return {k: int(fetch_scalar(c, f"SELECT count(*) FROM {k}") or 0) for k in _EXPECTED}


def _wait(ep: Endpoint, name: str, timeout: int = 40) -> dict:
    deadline = time.time() + timeout
    got: dict = {}
    while time.time() < deadline:
        got = _counts(ep, name)
        if got == _EXPECTED:
            return got
        time.sleep(1)
    return got


def _ctx(source, target, name, *, spread, strategy, n) -> Context:
    cfg = Config(
        source=source, target=target, project=f"it_{name}",
        scope=Scope(databases=[name]),
        slots=SlotConfig(strategy=strategy, n=n, max_slots=8, spread_partitions=spread),
    )
    return Context(config=cfg)


def _pub_via_root(source, name) -> list[bool]:
    with connect(source, name, read_only=True) as c:
        rows = fetch_all(c, "SELECT pubviaroot FROM pg_publication ORDER BY pubname")
    return [r["pubviaroot"] for r in rows]


def test_partition_non_spread_publishes_root_as_unit(
    source_endpoint, target_endpoint, partitioned_db
) -> None:
    name = partitioned_db
    ctx = _ctx(source_endpoint, target_endpoint, name,
               spread=False, strategy=SlotStrategy.SINGLE, n=1)

    with connect(source_endpoint, name, read_only=True) as c:
        leaves = {t.qualified for t in catalog.partition_map(c)[TableRef("public", "events")]}
    assert leaves == {"public.events_2023", "public.events_2024", "public.events_2025"}

    plan = build_cluster_plan(ctx.config, "run")
    tables = {t.qualified for s in plan.databases[0].slots for t in s.tables}
    assert "public.events" in tables and not (leaves & tables)  # leaves not standalone

    try:
        run_setup(ctx, force=False)
        assert all(_pub_via_root(source_endpoint, name))  # publish via root
        assert _wait(target_endpoint, name) == _EXPECTED   # data lands via root
    finally:
        run_teardown(ctx, confirm=True)


def test_partition_spread_distributes_leaves(
    source_endpoint, target_endpoint, partitioned_db
) -> None:
    name = partitioned_db
    ctx = _ctx(source_endpoint, target_endpoint, name,
               spread=True, strategy=SlotStrategy.BALANCED, n=3)

    plan = build_cluster_plan(ctx.config, "run")
    specs = plan.databases[0].slots
    tables = {t.qualified for s in specs for t in s.tables}
    assert "public.events" not in tables  # root replaced by leaves
    assert {"public.events_2023", "public.events_2024", "public.events_2025"} <= tables
    assert all(not s.via_partition_root for s in specs)

    try:
        run_setup(ctx, force=False)
        assert not any(_pub_via_root(source_endpoint, name))  # leaf identity, not root
        assert _wait(target_endpoint, name) == _EXPECTED       # all leaf data lands
    finally:
        run_teardown(ctx, confirm=True)
