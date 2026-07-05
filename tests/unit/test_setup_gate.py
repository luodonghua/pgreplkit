"""Unit tests for the setup preflight gate classification (H2)."""

from __future__ import annotations

import pytest

from pgreplkit.checks.results import CheckReport, CheckResult, Level
from pgreplkit.errors import PreflightBlocked
from pgreplkit.phases.setup import _gate_preflight


def _report(*codes: str) -> CheckReport:
    r = CheckReport()
    for code in codes:
        r.add(CheckResult(Level.BLOCK, code, f"{code} failed"))
    return r


def test_no_blocks_returns_empty() -> None:
    r = CheckReport()
    r.add(CheckResult(Level.WARN, "sequences", "info"))
    assert _gate_preflight(r, force=False, force_correctness=False) == []


def test_capacity_block_requires_force() -> None:
    r = _report("max_replication_slots")
    with pytest.raises(PreflightBlocked):
        _gate_preflight(r, force=False, force_correctness=False)
    overrides = _gate_preflight(r, force=True, force_correctness=False)
    assert [o["code"] for o in overrides] == ["max_replication_slots"]
    assert overrides[0]["category"] == "capacity"


def test_correctness_block_not_bypassed_by_plain_force() -> None:
    r = _report("replica_identity")
    # plain --force must NOT bypass a correctness/data-loss block
    with pytest.raises(PreflightBlocked):
        _gate_preflight(r, force=True, force_correctness=False)
    overrides = _gate_preflight(r, force=True, force_correctness=True)
    assert overrides[0]["category"] == "correctness"


def test_mixed_blocks_need_both_flags() -> None:
    r = _report("max_wal_senders", "encoding_mismatch")
    with pytest.raises(PreflightBlocked):
        _gate_preflight(r, force=True, force_correctness=False)  # correctness still blocks
    overrides = _gate_preflight(r, force=True, force_correctness=True)
    cats = {o["code"]: o["category"] for o in overrides}
    assert cats["max_wal_senders"] == "capacity"
    assert cats["encoding_mismatch"] == "correctness"
