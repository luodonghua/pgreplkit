"""Pure comparison of *existing* publications/subscriptions against the computed plan.

Idempotency must adopt an existing object only if it actually matches the plan — not
by name alone (FR-41). These functions take catalog facts + the plan spec and return a
list of human-readable conflict strings (empty == safe to adopt). They are pure so they
can be unit-tested without a database.
"""

from __future__ import annotations

from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec


def publication_conflicts(
    spec: SlotSpec,
    *,
    tables: set[TableRef],
    publish_ops: set[str],
    via_partition_root: bool,
) -> list[str]:
    """Compare an existing publication's facts to the plan (FR-41)."""
    conflicts: list[str] = []
    if tables != set(spec.tables):
        missing = {t.qualified for t in spec.tables} - {t.qualified for t in tables}
        extra = {t.qualified for t in tables} - {t.qualified for t in spec.tables}
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if extra:
            detail.append(f"unexpected {sorted(extra)}")
        conflicts.append(f"table set differs ({'; '.join(detail)})")
    if publish_ops != set(spec.publish_ops):
        conflicts.append(
            f"publish ops differ (have {sorted(publish_ops)}, "
            f"plan {sorted(set(spec.publish_ops))})"
        )
    if via_partition_root != spec.via_partition_root:
        conflicts.append(
            f"publish_via_partition_root differs (have {via_partition_root}, "
            f"plan {spec.via_partition_root})"
        )
    return conflicts


def _conninfo_endpoint(conninfo: str | None) -> tuple[str | None, str | None, str | None]:
    """(host, port, dbname) parsed from a libpq conninfo string; (None,..) if unknown."""
    if not conninfo:
        return (None, None, None)
    try:
        from psycopg.conninfo import conninfo_to_dict

        d = conninfo_to_dict(conninfo)
    except Exception:  # noqa: BLE001
        return (None, None, None)
    port = d.get("port")
    return (d.get("host"), str(port) if port is not None else None, d.get("dbname"))


def subscription_conflicts(
    spec: SlotSpec,
    *,
    slot_name: str | None,
    publications: list[str] | None,
    conninfo: str | None,
    expected_conninfo: str,
) -> list[str]:
    """Compare an existing subscription's facts to the plan (FR-41).

    Compares the slot name, that the plan's publication is subscribed, and the
    subscriber's connection target (host/port/dbname). The connection is only compared
    when the catalog exposes ``subconninfo`` (privileged); otherwise it is skipped.
    """
    conflicts: list[str] = []
    if slot_name is not None and slot_name != spec.name:
        conflicts.append(f"slot_name differs (have '{slot_name}', plan '{spec.name}')")
    if publications is not None and spec.name not in publications:
        conflicts.append(
            f"subscription publications {publications} do not include '{spec.name}'"
        )
    have = _conninfo_endpoint(conninfo)
    want = _conninfo_endpoint(expected_conninfo)
    if have[0] is not None and want[0] is not None:
        # only compare the fields we can resolve on both sides
        for label, h, w in zip(("host", "port", "dbname"), have, want, strict=False):
            if h is not None and w is not None and h != w:
                conflicts.append(f"connection {label} differs (have '{h}', plan '{w}')")
    return conflicts
