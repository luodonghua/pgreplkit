"""Connection management: psycopg3 connections with timeouts and read-only mode.

Each worker thread must own its own connection (psycopg3 connections are not safe for
concurrent use) — see DESIGN.md §9.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from pgreplkit.config.models import Endpoint
from pgreplkit.core import dns
from pgreplkit.errors import ConnectionFailed
from pgreplkit.logconf import get_logger, redact

log = get_logger()


@contextmanager
def connect(
    endpoint: Endpoint,
    dbname: str | None = None,
    *,
    read_only: bool = False,
    statement_timeout_ms: int = 0,
    lock_timeout_ms: int = 0,
    autocommit: bool = True,
) -> Iterator[psycopg.Connection]:
    """Open a psycopg3 connection to ``endpoint`` (optionally a specific db).

    Applies statement/lock timeouts (FR-39, NFR-10) and optional read-only mode
    (defense-in-depth for read-only phases, FR-31).
    """
    dsn = endpoint.dsn(dbname)
    # Resolve managed/other hosts with a short client TTL and pin hostaddr so we
    # re-resolve often and avoid stale cached IPs (host is kept for TLS SNI).
    ip = dns.resolve(endpoint.host, endpoint.dns_ttl)
    if ip is not None:
        from psycopg.conninfo import make_conninfo

        dsn = make_conninfo(dsn, hostaddr=ip)
    try:
        conn = psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        # Never leak the DSN (which may contain a password).
        target = f"{endpoint.host}:{endpoint.port}/{dbname or endpoint.dbname or ''}"
        raise ConnectionFailed(
            f"could not connect to {target}: {redact(str(exc)).strip()}",
            hint="check host/port/credentials, security groups, and pg_hba.conf",
        ) from exc
    try:
        if read_only:
            # Defense-in-depth for read-only phases (FR-31).
            conn.execute("SET default_transaction_read_only = on")
        if statement_timeout_ms > 0:
            conn.execute(f"SET statement_timeout = {int(statement_timeout_ms)}")
        if lock_timeout_ms > 0:
            conn.execute(f"SET lock_timeout = {int(lock_timeout_ms)}")
        yield conn
    finally:
        conn.close()


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetch_scalar(conn: psycopg.Connection, sql: str, params: Any = None) -> Any:
    row = fetch_one(conn, sql, params)
    if row is None:
        return None
    return next(iter(row.values()))
