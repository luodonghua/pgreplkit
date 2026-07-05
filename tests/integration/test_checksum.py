"""Integration test for validate checksums (REVIEW H2): content divergence with equal
row counts must be detected. Requires source + target PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest

from pgreplkit.config.models import Endpoint
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.model import TableRef

pytestmark = pytest.mark.integration


@pytest.fixture()
def two_dbs(source_endpoint: Endpoint, target_endpoint: Endpoint):
    name = f"pgrk_it_chk_{uuid.uuid4().hex[:8]}"
    for ep in (source_endpoint, target_endpoint):
        with connect(ep, "postgres", autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
        with connect(ep, name, autocommit=True) as c:
            c.execute("CREATE TABLE t (id int PRIMARY KEY, v text)")
    try:
        yield name
    finally:
        for ep in (source_endpoint, target_endpoint):
            with connect(ep, "postgres", autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_checksum_detects_divergence_with_equal_counts(
    source_endpoint, target_endpoint, two_dbs
) -> None:
    name = two_dbs
    t = TableRef("public", "t")
    with connect(source_endpoint, name, autocommit=True) as c:
        c.execute("INSERT INTO t SELECT g, 'v'||g FROM generate_series(1,100) g")
    with connect(target_endpoint, name, autocommit=True) as c:
        # same 100 rows but id=42 has different content
        c.execute("INSERT INTO t SELECT g, 'v'||g FROM generate_series(1,100) g")
        c.execute("UPDATE t SET v='TAMPERED' WHERE id=42")

    with connect(source_endpoint, name, read_only=True) as sc, \
         connect(target_endpoint, name, read_only=True) as tc:
        # counts match
        assert catalog.row_count(sc, t) == catalog.row_count(tc, t) == 100
        # full checksum differs (catches the tampered row)
        assert catalog.table_checksum(sc, t, sample=False) != \
               catalog.table_checksum(tc, t, sample=False)

        # fix the row -> checksums match (full and sampled)
    with connect(target_endpoint, name, autocommit=True) as c:
        c.execute("UPDATE t SET v='v42' WHERE id=42")
    with connect(source_endpoint, name, read_only=True) as sc, \
         connect(target_endpoint, name, read_only=True) as tc:
        assert catalog.table_checksum(sc, t, sample=False) == \
               catalog.table_checksum(tc, t, sample=False)
        assert catalog.table_checksum(sc, t, sample=True) == \
               catalog.table_checksum(tc, t, sample=True)
