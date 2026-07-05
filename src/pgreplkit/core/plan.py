"""Build a concrete, named replication plan from topology + slot planning (FR-36/43).

Pure-ish: reads the source catalog (read-only) to gather tables/weights/FKs, then uses
the pure slot planner to assign tables to named slots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pgreplkit.config.models import Config, SlotStrategy
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.matching import in_scope
from pgreplkit.core.model import TableInput, TableRef
from pgreplkit.core.naming import object_base
from pgreplkit.core.slotplan import plan_database_slots
from pgreplkit.core.topology import discover_topology

DEFAULT_PUBLISH_OPS = ("insert", "update", "delete", "truncate")


@dataclass(frozen=True)
class SlotSpec:
    db: str
    index: int
    name: str
    tables: tuple[TableRef, ...]
    publish_ops: tuple[str, ...] = DEFAULT_PUBLISH_OPS
    via_partition_root: bool = False


@dataclass
class DatabasePlan:
    db: str
    slots: list[SlotSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ClusterPlan:
    run_id: str
    databases: list[DatabasePlan] = field(default_factory=list)

    @property
    def total_slots(self) -> int:
        return sum(len(d.slots) for d in self.databases)


def _replicable_relations(
    relations: list[catalog.RelationInfo], scope
) -> list[catalog.RelationInfo]:
    """In-scope permanent tables/partitioned roots, excluding child partitions.

    Leaf/intermediate partitions (``relispartition``) are represented by their
    top-level partitioned root, so they are not treated as standalone units here
    (avoids double-publishing the same rows, FR-11).
    """
    out = []
    for r in relations:
        if r.relkind not in ("r", "p"):
            continue
        if r.persistence != "p":  # only permanent tables
            continue
        if r.is_partition:  # covered by its partitioned root
            continue
        if not in_scope(r.schema, scope.include_schemas, scope.exclude_schemas):
            continue
        if not in_scope(r.name, scope.include_tables, scope.exclude_tables):
            continue
        out.append(r)
    return out


def build_cluster_plan(cfg: Config, run_id: str) -> ClusterPlan:
    plan = ClusterPlan(run_id=run_id)
    topo = discover_topology(cfg.source, cfg.scope)
    spread = cfg.slots.spread_partitions

    manual_maps: dict[str, dict] = {}
    if cfg.slots.strategy == SlotStrategy.MANUAL and cfg.slots.slot_map is not None:
        from pgreplkit.config.slotmap import load_slot_map

        manual_maps = load_slot_map(cfg.slots.slot_map)

    for ds in topo.in_scope:
        with connect(cfg.source, ds.name, read_only=True) as conn:
            relations = catalog.list_relations(conn)
            weights = catalog.table_weights(conn)
            edges = catalog.fk_edges(conn)
            partmap = catalog.partition_map(conn)

        rels = _replicable_relations(relations, cfg.scope)
        if not rels:
            continue

        # partitioned roots in scope -> the set of refs that must publish via the root
        # (non-spread) or expand into leaves (spread). Roots with no discoverable
        # leaves fall back to plain-table behaviour.
        partitioned_roots: set[TableRef] = set()
        tables: list[TableInput] = []
        for r in rels:
            if r.is_partitioned_root and partmap.get(r.ref):
                leaves = partmap[r.ref]
                pweights = tuple(weights.get(leaf, 0.0) for leaf in leaves)
                tables.append(
                    TableInput(
                        ref=r.ref,
                        weight=sum(pweights),  # root's own rows are always 0
                        partitions=tuple(leaves),
                        partition_weights=pweights,
                    )
                )
                partitioned_roots.add(r.ref)
            else:
                tables.append(TableInput(ref=r.ref, weight=weights.get(r.ref, 0.0)))

        tables.sort(key=lambda ti: ti.ref)
        unit_refs = {ti.ref for ti in tables}
        # keep only FK edges where both ends are in-scope planning units
        scoped_edges = [(a, b) for a, b in edges if a in unit_refs and b in unit_refs]

        db_slot_plan = plan_database_slots(
            ds.name, tables, scoped_edges, cfg.slots, manual_map=manual_maps.get(ds.name)
        )

        dbp = DatabasePlan(db=ds.name, warnings=list(db_slot_plan.warnings))
        for assignment in db_slot_plan.slots:
            if not assignment.tables:
                continue
            name = object_base(cfg.object_prefix, run_id, ds.name, assignment.index)
            # publish via the partition root (one unit) unless we're spreading a
            # partitioned table's leaves across slots (FR-11). Only set the option when
            # the slot actually contains a partitioned root, so SQL for non-partitioned
            # setups is unchanged.
            via_root = (not spread) and any(
                t in partitioned_roots for t in assignment.tables
            )
            dbp.slots.append(
                SlotSpec(
                    db=ds.name,
                    index=assignment.index,
                    name=name,
                    tables=assignment.tables,
                    publish_ops=tuple(cfg.slots.publish),
                    via_partition_root=via_root,
                )
            )
        plan.databases.append(dbp)

    return plan
