"""Manual slot-map loader (FR-12).

Parses a YAML file describing an explicit per-database slot->table mapping, used with
`--slots manual`. Shape:

    databases:
      appdb:
        slots:
          - name: hot
            tables: [public.orders, public.order_items]
          - name: rest
            tables: ["*"]        # catch-all for the remaining in-scope tables
"""

from __future__ import annotations

from pathlib import Path

import yaml

from pgreplkit.errors import ConfigError


def load_slot_map(path: Path) -> dict[str, dict[str, list[str]]]:
    """Return {db: {slot_name: [table_patterns]}} from the YAML file."""
    if not path.exists():
        raise ConfigError(f"slot-map file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in slot-map {path}: {exc}") from exc

    dbs = raw.get("databases")
    if not isinstance(dbs, dict):
        raise ConfigError("slot-map must have a top-level 'databases' mapping (FR-12)")

    out: dict[str, dict[str, list[str]]] = {}
    for db, spec in dbs.items():
        slots = (spec or {}).get("slots")
        if not isinstance(slots, list) or not slots:
            raise ConfigError(f"slot-map database '{db}' must list at least one slot")
        mapping: dict[str, list[str]] = {}
        for s in slots:
            name = s.get("name")
            tables = s.get("tables")
            if not name or not isinstance(tables, list):
                raise ConfigError(
                    f"slot-map database '{db}' has a slot missing name/tables"
                )
            mapping[str(name)] = [str(t) for t in tables]
        out[str(db)] = mapping
    return out
