"""Read-only catalog queries (FR-31). No statement here modifies the cluster.

Grouped by need: database enumeration, relation listing, table weights, FK edges.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from pgreplkit.core.connection import fetch_all, fetch_scalar
from pgreplkit.core.model import TableRef

# Schemas that are never in scope.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


@dataclass(frozen=True)
class DatabaseInfo:
    name: str
    is_template: bool
    allow_conn: bool
    encoding: str
    collate: str
    ctype: str


@dataclass(frozen=True)
class RelationInfo:
    schema: str
    name: str
    relkind: str          # r=table, p=partitioned, v=view, m=matview, f=foreign
    persistence: str      # p=permanent, u=unlogged, t=temp
    replica_identity: str  # d=default, n=nothing, f=full, i=index
    has_pk: bool
    has_unique: bool
    is_partition: bool = False  # relispartition: this relation is a partition of another

    @property
    def ref(self) -> TableRef:
        return TableRef(self.schema, self.name)

    @property
    def is_ordinary_table(self) -> bool:
        return self.relkind in ("r", "p")

    @property
    def is_partitioned_root(self) -> bool:
        """A top-level partitioned table (declarative partitioning parent)."""
        return self.relkind == "p" and not self.is_partition


def list_databases(conn: psycopg.Connection) -> list[DatabaseInfo]:
    rows = fetch_all(
        conn,
        """
        SELECT d.datname AS name,
               d.datistemplate AS is_template,
               d.datallowconn AS allow_conn,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collate,
               d.datctype AS ctype
        FROM pg_database d
        ORDER BY d.datname
        """,
    )
    return [DatabaseInfo(**r) for r in rows]


def count_user_tables(conn: psycopg.Connection) -> int:
    """Count ordinary/partitioned tables outside system schemas (FR-6)."""
    return int(
        fetch_scalar(
            conn,
            """
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
              AND n.nspname NOT LIKE 'pg_toast_temp%'
            """,
        )
        or 0
    )


def list_relations(conn: psycopg.Connection) -> list[RelationInfo]:
    """List all relations outside system schemas with replica-identity facts."""
    rows = fetch_all(
        conn,
        """
        SELECT n.nspname AS schema,
               c.relname AS name,
               c.relkind::text AS relkind,
               c.relpersistence::text AS persistence,
               c.relreplident::text AS replica_identity,
               c.relispartition AS is_partition,
               EXISTS (
                   SELECT 1 FROM pg_index i
                   WHERE i.indrelid = c.oid AND i.indisprimary
               ) AS has_pk,
               EXISTS (
                   SELECT 1 FROM pg_index i
                   WHERE i.indrelid = c.oid AND i.indisunique AND NOT i.indisprimary
               ) AS has_unique
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND n.nspname NOT LIKE 'pg_temp%'
          AND n.nspname NOT LIKE 'pg_toast_temp%'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        ORDER BY n.nspname, c.relname
        """,
    )
    return [RelationInfo(**r) for r in rows]


def table_weights(conn: psycopg.Connection) -> dict[TableRef, float]:
    """Point-in-time write activity per table (FR-11): n_tup_ins+upd+del."""
    rows = fetch_all(
        conn,
        """
        SELECT schemaname AS schema, relname AS name,
               (n_tup_ins + n_tup_upd + n_tup_del)::float8 AS weight
        FROM pg_stat_all_tables
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        """,
    )
    return {TableRef(r["schema"], r["name"]): float(r["weight"] or 0.0) for r in rows}


def fk_edges(conn: psycopg.Connection) -> list[tuple[TableRef, TableRef]]:
    """Foreign-key edges between tables (FR-11 affinity grouping)."""
    rows = fetch_all(
        conn,
        """
        SELECT cn.nspname AS child_schema, cc.relname AS child_name,
               pn.nspname AS parent_schema, pc.relname AS parent_name
        FROM pg_constraint con
        JOIN pg_class cc ON cc.oid = con.conrelid
        JOIN pg_namespace cn ON cn.oid = cc.relnamespace
        JOIN pg_class pc ON pc.oid = con.confrelid
        JOIN pg_namespace pn ON pn.oid = pc.relnamespace
        WHERE con.contype = 'f'
        """,
    )
    return [
        (
            TableRef(r["child_schema"], r["child_name"]),
            TableRef(r["parent_schema"], r["parent_name"]),
        )
        for r in rows
    ]


def partition_map(conn: psycopg.Connection) -> dict[TableRef, list[TableRef]]:
    """Map each top-level partitioned table to its **leaf** partitions (FR-11).

    Uses ``pg_partition_tree`` so multi-level (sub-partitioned) hierarchies collapse to
    their real leaves. Only top-level roots (``NOT relispartition``) are keys; leaves
    may themselves be nested any number of levels deep. Empty for non-partitioned
    schemas. Leaves in system schemas are ignored.
    """
    rows = fetch_all(
        conn,
        """
        SELECT rn.nspname AS root_schema, rc.relname AS root_name,
               ln.nspname AS leaf_schema, lc.relname AS leaf_name
        FROM pg_partitioned_table pt
        JOIN pg_class rc ON rc.oid = pt.partrelid
        JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        JOIN LATERAL pg_partition_tree(rc.oid) t ON t.isleaf
        JOIN pg_class lc ON lc.oid = t.relid
        JOIN pg_namespace ln ON ln.oid = lc.relnamespace
        WHERE NOT rc.relispartition
          AND rn.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
          AND ln.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
        ORDER BY 1, 2, 3, 4
        """,
    )
    out: dict[TableRef, list[TableRef]] = {}
    for r in rows:
        root = TableRef(r["root_schema"], r["root_name"])
        out.setdefault(root, []).append(TableRef(r["leaf_schema"], r["leaf_name"]))
    return out


def get_settings(conn: psycopg.Connection, names: list[str]) -> dict[str, str | None]:
    """Fetch pg_settings values for the given names (missing -> None)."""
    rows = fetch_all(
        conn,
        "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
        (names,),
    )
    found = {r["name"]: r["setting"] for r in rows}
    return {n: found.get(n) for n in names}


def count_sequences(conn: psycopg.Connection, schemas: set[str] | None = None) -> int:
    if schemas is not None:
        return int(
            fetch_scalar(
                conn,
                """
                SELECT count(*) FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'S' AND n.nspname = ANY(%s)
                """,
                (list(schemas),),
            )
            or 0
        )
    return int(
        fetch_scalar(
            conn,
            """
            SELECT count(*) FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'S'
              AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            """,
        )
        or 0
    )


def table_exists(conn: psycopg.Connection, table: TableRef) -> bool:
    return bool(
        fetch_scalar(
            conn,
            """
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
            """,
            (table.schema, table.name),
        )
    )


def count_large_objects(conn: psycopg.Connection) -> int:
    return int(fetch_scalar(conn, "SELECT count(*) FROM pg_largeobject_metadata") or 0)


def current_slot_count(conn: psycopg.Connection) -> int:
    return int(fetch_scalar(conn, "SELECT count(*) FROM pg_replication_slots") or 0)


def database_info(conn: psycopg.Connection, dbname: str) -> DatabaseInfo | None:
    rows = fetch_all(
        conn,
        """
        SELECT d.datname AS name, d.datistemplate AS is_template,
               d.datallowconn AS allow_conn,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collate, d.datctype AS ctype
        FROM pg_database d WHERE d.datname = %s
        """,
        (dbname,),
    )
    return DatabaseInfo(**rows[0]) if rows else None


def publication_exists(conn: psycopg.Connection, name: str) -> bool:
    return bool(
        fetch_scalar(conn, "SELECT 1 FROM pg_publication WHERE pubname = %s", (name,))
    )


def publication_tables(conn: psycopg.Connection, name: str) -> set[TableRef]:
    rows = fetch_all(
        conn,
        """
        SELECT schemaname AS schema, tablename AS name
        FROM pg_publication_tables WHERE pubname = %s
        """,
        (name,),
    )
    return {TableRef(r["schema"], r["name"]) for r in rows}


def subscription_exists(conn: psycopg.Connection, name: str) -> bool:
    return bool(
        fetch_scalar(conn, "SELECT 1 FROM pg_subscription WHERE subname = %s", (name,))
    )


def publication_details(conn: psycopg.Connection, name: str) -> dict | None:
    """Publish operations + publish_via_partition_root for an existing publication.

    ``pubviaroot`` exists on PG13+; on older servers it is absent and reported False.
    Returns None if the publication does not exist.
    """
    from pgreplkit.core.connection import fetch_one

    row = fetch_one(conn, "SELECT * FROM pg_publication WHERE pubname = %s", (name,))
    if row is None:
        return None
    ops = {
        "insert": row.get("pubinsert"),
        "update": row.get("pubupdate"),
        "delete": row.get("pubdelete"),
        "truncate": row.get("pubtruncate"),
    }
    return {
        "publish": {op for op, on in ops.items() if on},
        "via_partition_root": bool(row.get("pubviaroot", False)),
    }


def subscription_details(conn: psycopg.Connection, name: str) -> dict | None:
    """Slot name, subscribed publications, enabled state and connection for a
    subscription. ``conninfo`` requires a privileged role; None if unavailable.
    Returns None if the subscription does not exist."""
    from pgreplkit.core.connection import fetch_one

    row = fetch_one(
        conn,
        """
        SELECT subenabled, subslotname, subpublications, subconninfo
        FROM pg_subscription WHERE subname = %s
        """,
        (name,),
    )
    if row is None:
        return None
    return {
        "enabled": row.get("subenabled"),
        "slot_name": row.get("subslotname"),
        "publications": list(row.get("subpublications") or []),
        "conninfo": row.get("subconninfo"),
    }


def list_roles(conn: psycopg.Connection) -> set[str]:
    """Non-system role names (FR-32/61a). Excludes pg_* built-ins."""
    rows = fetch_all(
        conn,
        "SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'",
    )
    return {r["rolname"] for r in rows}


def role_details(conn: psycopg.Connection) -> dict[str, dict]:
    """Role attributes needed to recreate roles on the target (FR-33)."""
    rows = fetch_all(
        conn,
        """
        SELECT rolname AS name, rolcanlogin AS can_login, rolcreatedb AS createdb,
               rolcreaterole AS createrole
        FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'
        """,
    )
    return {r["name"]: r for r in rows}


def non_default_tablespaces(conn: psycopg.Connection) -> set[str]:
    """Tablespaces used by in-scope objects, excluding the defaults (FR-34)."""
    rows = fetch_all(
        conn,
        """
        SELECT DISTINCT t.spcname AS name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_tablespace t ON t.oid = c.reltablespace
        WHERE c.reltablespace <> 0
          AND t.spcname NOT IN ('pg_default', 'pg_global')
          AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
        """,
    )
    return {r["name"] for r in rows}


def list_sequences(conn: psycopg.Connection) -> list[TableRef]:
    rows = fetch_all(
        conn,
        """
        SELECT schemaname AS schema, sequencename AS name
        FROM pg_sequences
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY 1, 2
        """,
    )
    return [TableRef(r["schema"], r["name"]) for r in rows]


def sequence_last_value(conn: psycopg.Connection, seq: TableRef) -> int | None:
    return fetch_scalar(conn, f"SELECT last_value FROM {seq.quoted}")


def row_count(conn: psycopg.Connection, table: TableRef) -> int:
    return int(fetch_scalar(conn, f"SELECT count(*) FROM {table.quoted}") or 0)


def table_columns(conn: psycopg.Connection, table: TableRef) -> list[tuple]:
    """Column signature (name, type, nullable) in ordinal order — for FR-18/19 target
    schema/column compatibility. Empty list if the table does not exist."""
    rows = fetch_all(
        conn,
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table.schema, table.name),
    )
    return [(r["column_name"], r["data_type"], r["is_nullable"]) for r in rows]


def table_checksum(conn: psycopg.Connection, table: TableRef, *, sample: bool) -> str | None:
    """Order-independent content checksum of a table (FR-61).

    Hashes each row's text, orders the per-row hashes, and hashes the concatenation, so
    it is independent of physical row order and needs no knowledge of the primary key.
    ``sample`` restricts to ~1/8 of rows (by row-hash prefix) for a cheap sampled check.
    Note: row text can differ for exotic types across major versions; suitable for the
    common column types.
    """
    where = "WHERE left(md5(t::text), 1) IN ('0', '1')" if sample else ""
    sql = (
        "SELECT md5(coalesce(string_agg(rmd5, ',' ORDER BY rmd5), '')) "
        f"FROM (SELECT md5(t::text) AS rmd5 FROM {table.quoted} t {where}) s"
    )
    return fetch_scalar(conn, sql)


def object_counts(conn: psycopg.Connection) -> dict[str, int]:
    """Counts of replicable-relevant objects for validation (FR-61a)."""
    tables = int(
        fetch_scalar(
            conn,
            """
            SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','p')
              AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
            """,
        )
        or 0
    )
    seqs = count_sequences(conn)
    los = count_large_objects(conn)
    return {"tables": tables, "sequences": seqs, "large_objects": los}
