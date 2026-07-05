"""End-to-end integration test: setup (copy) -> status -> teardown.

Requires source AND target live PostgreSQL with logical WAL, plus the target able to
reach the source (advertised host/port). Skips if env not set.
"""

from __future__ import annotations

import time
import uuid

import pytest

from pgreplkit.config.models import Config, Endpoint, Scope, SlotConfig, SlotStrategy
from pgreplkit.context import Context
from pgreplkit.core.connection import connect, fetch_scalar
from pgreplkit.phases.setup import run_setup
from pgreplkit.phases.status import gather_status
from pgreplkit.phases.teardown import run_teardown

pytestmark = pytest.mark.integration


@pytest.fixture()
def replicated_db(source_endpoint: Endpoint, target_endpoint: Endpoint, tmp_path, monkeypatch):
    monkeypatch.setenv("PGREPLKIT_HOME", str(tmp_path))
    name = f"pgrk_it_e2e_{uuid.uuid4().hex[:8]}"
    # source: schema (serial -> sequence) + data
    with connect(source_endpoint, "postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with connect(source_endpoint, name, autocommit=True) as c:
        c.execute("CREATE TABLE t (id serial PRIMARY KEY, v text)")
        # insert without id so the sequence advances to 50 on the source
        c.execute("INSERT INTO t (v) SELECT 'v'||g FROM generate_series(1,50) g")
    # target: same schema, empty (sequence still at 1 until sync-sequences)
    with connect(target_endpoint, "postgres", autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    with connect(target_endpoint, name, autocommit=True) as c:
        c.execute("CREATE TABLE t (id serial PRIMARY KEY, v text)")
    try:
        yield name
    finally:
        with connect(target_endpoint, name, autocommit=True) as c:
            c.execute("DROP SUBSCRIPTION IF EXISTS placeholder")  # no-op safety
        for ep in (source_endpoint, target_endpoint):
            with connect(ep, "postgres", autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _ctx(source, target, dbname) -> Context:
    cfg = Config(
        source=source,
        target=target,
        project=f"it_{dbname}",
        scope=Scope(databases=[dbname]),
        slots=SlotConfig(strategy=SlotStrategy.SINGLE),
    )
    return Context(config=cfg)


def test_setup_copy_status_teardown(
    source_endpoint, target_endpoint, replicated_db
) -> None:
    name = replicated_db
    ctx = _ctx(source_endpoint, target_endpoint, name)

    # setup with copy strategy
    manifest = run_setup(ctx, force=False)
    assert len(manifest.slots) == 1

    # wait for initial copy to complete, then assert data + status
    deadline = time.time() + 30
    target_rows = 0
    while time.time() < deadline:
        with connect(target_endpoint, name, read_only=True) as c:
            target_rows = int(fetch_scalar(c, "SELECT count(*) FROM t") or 0)
        if target_rows == 50:
            break
        time.sleep(1)
    assert target_rows == 50, f"expected 50 rows copied, got {target_rows}"

    status = gather_status(ctx)
    assert len(status) == 1
    assert status[0].synced
    assert status[0].slot_active

    # teardown removes everything
    slot_name = manifest.slots[0].name
    run_teardown(ctx, confirm=True)
    with connect(target_endpoint, name, read_only=True) as c:
        assert fetch_scalar(
            c, "SELECT count(*) FROM pg_subscription WHERE subname = %s", (slot_name,)
        ) == 0
    with connect(source_endpoint, name, read_only=True) as c:
        assert fetch_scalar(
            c, "SELECT count(*) FROM pg_publication WHERE pubname = %s", (slot_name,)
        ) == 0
        assert (
            fetch_scalar(
                c, "SELECT count(*) FROM pg_replication_slots WHERE slot_name = %s",
                (slot_name,),
            )
            == 0
        )


def _wait_synced(target_endpoint, name, expected=50, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with connect(target_endpoint, name, read_only=True) as c:
            if int(fetch_scalar(c, "SELECT count(*) FROM t") or 0) == expected:
                return
        time.sleep(1)
    raise AssertionError("initial copy did not complete in time")


def test_validate_sequences_ready_cutover(
    source_endpoint, target_endpoint, replicated_db
) -> None:
    from pgreplkit.config.models import ValidateDepth
    from pgreplkit.phases.cutover import run_cutover
    from pgreplkit.phases.ready import run_ready
    from pgreplkit.phases.sequences import run_sync_sequences
    from pgreplkit.phases.validate import run_validate

    name = replicated_db
    ctx = _ctx(source_endpoint, target_endpoint, name)
    try:
        run_setup(ctx, force=False)
        _wait_synced(target_endpoint, name)

        # validate: rows + objects match
        report = run_validate(ctx, ValidateDepth.SAMPLED)
        assert not report.has_blocks, [r.message for r in report.blocks]

        # ready: lag 0, synced, active
        assert run_ready(ctx).passed

        # sequences: target seq behind (1) until synced; after sync it matches source (50)
        with connect(target_endpoint, name, read_only=True) as c:
            before = int(fetch_scalar(c, "SELECT last_value FROM t_id_seq") or 0)
        assert before < 50
        run_sync_sequences(ctx)
        with connect(target_endpoint, name, read_only=True) as c:
            after = int(fetch_scalar(c, "SELECT last_value FROM t_id_seq") or 0)
        assert after == 50

        # cutover orchestration (writes already stopped: no ongoing writes in test)
        result = run_cutover(ctx, writes_stopped=True, drain_timeout_s=30)
        assert result.signalled
        assert any("READY FOR CUTOVER" in s for s in result.steps)
    finally:
        from pgreplkit.phases.teardown import run_teardown

        run_teardown(ctx, confirm=True)
