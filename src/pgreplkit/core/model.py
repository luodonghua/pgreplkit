"""Shared value objects used across phases (catalog, slot planning, manifest)."""

from __future__ import annotations

from dataclasses import dataclass, field


def quote_ident(ident: str) -> str:
    """Quote a SQL identifier, doubling embedded quotes."""
    return '"' + ident.replace('"', '""') + '"'


@dataclass(frozen=True, order=True)
class TableRef:
    """A schema-qualified table reference."""

    schema: str
    name: str

    @property
    def qualified(self) -> str:
        """Dotted, unquoted form, e.g. ``public.orders`` (for display/matching)."""
        return f"{self.schema}.{self.name}"

    @property
    def quoted(self) -> str:
        """Safely quoted form for SQL, e.g. ``"public"."orders"``."""
        return f"{quote_ident(self.schema)}.{quote_ident(self.name)}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.qualified

    @classmethod
    def parse(cls, text: str) -> TableRef:
        """Parse ``schema.table`` (defaults schema to ``public`` if unqualified)."""
        if "." in text:
            schema, _, name = text.partition(".")
        else:
            schema, name = "public", text
        return cls(schema=schema, name=name)


@dataclass(frozen=True)
class TableInput:
    """A table plus the facts slot planning needs (no DB access here).

    For a partitioned table, ``partitions`` lists its leaf-partition refs and
    ``partition_weights`` (optional) the per-leaf write-activity weights aligned by
    index. When ``partition_weights`` is empty, partition spreading falls back to an
    even split of ``weight`` across the leaves.
    """

    ref: TableRef
    weight: float = 0.0
    partitions: tuple[TableRef, ...] = field(default_factory=tuple)
    partition_weights: tuple[float, ...] = field(default_factory=tuple)

    @property
    def is_partitioned(self) -> bool:
        return len(self.partitions) > 0
