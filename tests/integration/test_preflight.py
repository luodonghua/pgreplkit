"""Integration tests for preflight (FR-14..29). Require a live PostgreSQL."""

from __future__ import annotations

import uuid

import pytest

from pgreplkit.config.models import Config, Endpoint, Scope
from pgreplkit.context import Context
from pgreplkit.core.connection import connect
from pgreplkit.phases.preflight import run_preflight

pytestmark = pytest.mark.integration


@pytest.fixture()
def db_with_issues(source_endpoint: Endpoint):
    name = f"pgrk_it_pf_{uuid.uuid4().hex[:8]}"
    with connect(source_endpoint, "postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    with connect(source_endpoint, name, autocommit=True) as conn:
        conn.execute("CREATE TABLE good (id int PRIMARY KEY)")
        conn.execute("CREATE TABLE nopk (a int, b text)")          # replica-identity block
        conn.execute("CREATE VIEW v AS SELECT * FROM good")        # relation-kind block
        conn.execute("CREATE UNLOGGED TABLE u (id int PRIMARY KEY)")  # warn
    try:
        yield name
    finally:
        with connect(source_endpoint, "postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_preflight_detects_issues(source_endpoint: Endpoint, db_with_issues) -> None:
    name = db_with_issues
    cfg = Config(source=source_endpoint, scope=Scope(databases=[name]))
    report = run_preflight(Context(config=cfg))

    codes = {(r.code, r.subject) for r in report.results}
    # replica identity block on the no-PK table
    assert ("replica_identity", "public.nopk") in codes
    # view is a non-replicable relation kind
    assert ("relation_kind", "public.v") in codes
    # unlogged table warned
    assert ("unlogged_table", "public.u") in codes
    assert report.has_blocks
