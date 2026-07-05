"""SQL statement generators (pure). Return :class:`Sql` objects — never execute.

Identifiers are quoted; the subscription connection string is built from the source
endpoint. Passwords in CONNECTION are unavoidable (stored in pg_subscription, FR-45)
but are never logged (the Sql.note omits them).
"""

from __future__ import annotations

from pgreplkit.config.models import Endpoint
from pgreplkit.core.executor import Sql
from pgreplkit.core.model import quote_ident
from pgreplkit.core.plan import SlotSpec


def create_publication(spec: SlotSpec) -> Sql:
    from pgreplkit.config.models import ALLOWED_PUBLISH_OPS

    bad = [op for op in spec.publish_ops if op not in ALLOWED_PUBLISH_OPS]
    if bad:  # defense-in-depth against injection / typos (REVIEW M4)
        raise ValueError(f"invalid publish operation(s): {bad}")
    tables = ", ".join(t.quoted for t in spec.tables)
    ops = ", ".join(spec.publish_ops)
    with_opts = f"publish = '{ops}'"
    if spec.via_partition_root:
        with_opts += ", publish_via_partition_root = true"
    text = (
        f"CREATE PUBLICATION {quote_ident(spec.name)} "
        f"FOR TABLE {tables} WITH ({with_opts})"
    )
    return Sql(text, note=f"create publication {spec.name} ({len(spec.tables)} tables)",
               target="source")


def drop_publication(name: str) -> Sql:
    return Sql(
        f"DROP PUBLICATION IF EXISTS {quote_ident(name)}",
        note=f"drop publication {name}",
        target="source",
    )


def create_subscription(
    spec: SlotSpec,
    source: Endpoint,
    *,
    copy_data: bool,
    enabled: bool = True,
    create_slot: bool = True,
    disable_on_error: bool = True,
    origin: str | None = None,
    mask_password: bool = False,
) -> Sql:
    conninfo = source.dsn_for_subscriber(spec.db)
    if mask_password:
        # Structurally replace the password (guide output). Regex-redacting the final
        # SQL string is unsafe for libpq-quoted passwords (spaces/quotes/backslashes),
        # so mask on the parsed conninfo before literal-quoting it (H5).
        conninfo = _mask_conninfo_password(conninfo)
    opts = [
        f"copy_data = {'true' if copy_data else 'false'}",
        f"create_slot = {'true' if create_slot else 'false'}",
        f"enabled = {'true' if enabled else 'false'}",
        f"slot_name = {_quote_literal(spec.name)}",
    ]
    if disable_on_error:
        opts.append("disable_on_error = true")
    if origin is not None:
        # PG16+ loop avoidance for bidirectional setups (FR-72), e.g. origin='none'
        opts.append(f"origin = {_quote_literal(origin)}")
    text = (
        f"CREATE SUBSCRIPTION {quote_ident(spec.name)} "
        f"CONNECTION {_quote_literal(conninfo)} "
        f"PUBLICATION {quote_ident(spec.name)} "
        f"WITH ({', '.join(opts)})"
    )
    # note omits the conninfo (which contains a password)
    return Sql(text, note=f"create subscription {spec.name} (copy_data={copy_data})",
               target="target")


def drop_subscription(name: str, *, disable_first: bool = True) -> list[Sql]:
    stmts: list[Sql] = []
    if disable_first:
        stmts.append(
            Sql(f"ALTER SUBSCRIPTION {quote_ident(name)} DISABLE",
                note=f"disable subscription {name}", target="target")
        )
        # detach the slot so DROP doesn't try to reach a maybe-dead publisher
        stmts.append(
            Sql(f"ALTER SUBSCRIPTION {quote_ident(name)} SET (slot_name = NONE)",
                note=f"detach slot from {name}", target="target")
        )
    stmts.append(
        Sql(f"DROP SUBSCRIPTION IF EXISTS {quote_ident(name)}",
            note=f"drop subscription {name}", target="target")
    )
    return stmts


def drop_replication_slot(name: str) -> Sql:
    return Sql(
        "SELECT pg_drop_replication_slot(%s) "
        "WHERE EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = %s)",
        params=(name, name),
        note=f"drop orphaned replication slot {name}",
        target="source",
    )


def alter_publication_add_table(pub: str, table) -> Sql:
    return Sql(
        f"ALTER PUBLICATION {quote_ident(pub)} ADD TABLE {table.quoted}",
        note=f"add {table.qualified} to publication {pub}",
        target="source",
    )


def refresh_subscription(sub: str) -> Sql:
    return Sql(
        f"ALTER SUBSCRIPTION {quote_ident(sub)} REFRESH PUBLICATION",
        note=f"refresh subscription {sub}",
        target="target",
    )


def skip_transaction(sub: str, lsn: str) -> Sql:
    return Sql(
        f"ALTER SUBSCRIPTION {quote_ident(sub)} SKIP (lsn = {_quote_literal(lsn)})",
        note=f"skip LSN {lsn} on subscription {sub} (DATA LOSS)",
        target="target",
    )


def create_logical_slot(slot_name: str, plugin: str = "pgoutput") -> Sql:
    """Create the slot on the source BEFORE a snapshot so WAL is retained (FR-50)."""
    return Sql(
        "SELECT pg_create_logical_replication_slot(%s, %s)",
        params=(slot_name, plugin),
        note=f"create logical replication slot {slot_name} before snapshot",
        target="source",
    )


def enable_subscription(name: str) -> Sql:
    return Sql(
        f"ALTER SUBSCRIPTION {quote_ident(name)} ENABLE",
        note=f"enable subscription {name}",
        target="target",
    )


def _quote_literal(text: str) -> str:
    """Single-quote a SQL string literal, doubling embedded quotes."""
    return "'" + text.replace("'", "''") + "'"


def _mask_conninfo_password(conninfo: str) -> str:
    """Return a libpq conninfo with the password value replaced by ``***``.

    Parses the conninfo and re-serialises it, so masking is correct regardless of the
    original password's characters (spaces, quotes, backslashes, semicolons). Used for
    guide/runbook output where the DSN must never contain a real secret (H5).
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    try:
        params = conninfo_to_dict(conninfo)
    except Exception:  # noqa: BLE001 - be conservative: never emit the raw string
        return "<connection string redacted>"
    if "password" in params and params["password"] is not None:
        params["password"] = "***"
    return make_conninfo(**params)
