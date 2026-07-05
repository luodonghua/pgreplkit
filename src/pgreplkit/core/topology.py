"""Cluster topology discovery with skip rules (FR-4..9).

Enumerates databases/schemas/tables on the source and classifies each database as
in-scope or skipped (with a reason). Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pgreplkit.config.models import SYSTEM_DATABASES, Endpoint, Scope
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.matching import in_scope


@dataclass
class TableEntry:
    schema: str
    name: str
    relkind: str


@dataclass
class DatabaseScope:
    name: str
    included: bool
    reason: str | None = None
    table_count: int = 0
    schemas: list[str] = field(default_factory=list)
    tables: list[TableEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TopologyReport:
    databases: list[DatabaseScope] = field(default_factory=list)

    @property
    def in_scope(self) -> list[DatabaseScope]:
        return [d for d in self.databases if d.included]

    @property
    def skipped(self) -> list[DatabaseScope]:
        return [d for d in self.databases if not d.included]


def _system_skip_reason(db: catalog.DatabaseInfo, scope: Scope) -> str | None:
    if not scope.skip_system_dbs:
        return None
    if db.name in SYSTEM_DATABASES:
        return f"system database ({db.name})"
    if db.is_template:
        return "template database (datistemplate=true)"
    if not db.allow_conn:
        return "connections disallowed (datallowconn=false)"
    return None


def discover_topology(endpoint: Endpoint, scope: Scope) -> TopologyReport:
    """Discover databases/schemas/tables and classify scope (FR-4..9)."""
    report = TopologyReport()

    with connect(endpoint, endpoint.dbname or "postgres", read_only=True) as conn:
        databases = catalog.list_databases(conn)

    # explicit selection overrides discovery (FR-9), but system skips still apply
    explicit = set(scope.databases) if scope.databases else None

    for db in databases:
        # explicit selection filter
        if explicit is not None and db.name not in explicit:
            # not requested; skip silently unless it's the only info we have
            report.databases.append(
                DatabaseScope(db.name, included=False, reason="not selected")
            )
            continue

        reason = _system_skip_reason(db, scope)
        if reason is not None:
            report.databases.append(DatabaseScope(db.name, included=False, reason=reason))
            continue

        if not db.allow_conn:
            report.databases.append(
                DatabaseScope(db.name, included=False, reason="connections disallowed")
            )
            continue

        # connect into the database to inspect its relations
        ds = _inspect_database(endpoint, db.name, scope)
        report.databases.append(ds)

    return report


def _inspect_database(endpoint: Endpoint, dbname: str, scope: Scope) -> DatabaseScope:
    with connect(endpoint, dbname, read_only=True) as conn:
        relations = catalog.list_relations(conn)

    tables: list[TableEntry] = []
    schemas: set[str] = set()
    for rel in relations:
        if not rel.is_ordinary_table:
            continue
        if not in_scope(rel.schema, scope.include_schemas, scope.exclude_schemas):
            continue
        if not in_scope(rel.name, scope.include_tables, scope.exclude_tables):
            continue
        tables.append(TableEntry(rel.schema, rel.name, rel.relkind))
        schemas.add(rel.schema)

    ds = DatabaseScope(
        name=dbname,
        included=True,
        table_count=len(tables),
        schemas=sorted(schemas),
        tables=tables,
    )

    # FR-6: skip databases with no in-scope user tables
    if scope.skip_empty_dbs and not tables:
        ds.included = False
        ds.reason = "no in-scope user tables"

    # FR-7: warn (but keep) if the postgres admin DB holds user tables
    if dbname == "postgres" and tables:
        ds.warnings.append(
            "user tables found in the 'postgres' admin database — not a best practice; "
            "replicating anyway (exclude it explicitly to skip)"
        )

    return ds
