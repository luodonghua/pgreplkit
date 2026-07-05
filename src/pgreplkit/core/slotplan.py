"""Slot-allocation planning (pure — no DB access). Implements FR-10..13.

Strategies:
  - single      : one slot for all in-scope tables.
  - per-schema  : one slot per schema.
  - balanced    : FK-affinity grouping + weighted bin-packing into N slots (default).
  - manual      : explicit slot->table map (validated for coverage / ambiguity).

All facts (weights, FK edges, partitions) are passed in by the caller so this module
is deterministic and unit-testable without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pgreplkit.config.models import SlotConfig, SlotStrategy
from pgreplkit.core.model import TableInput, TableRef
from pgreplkit.errors import ConfigError, SlotCapExceeded

FkEdge = tuple[TableRef, TableRef]


@dataclass(frozen=True)
class SlotAssignment:
    index: int
    tables: tuple[TableRef, ...]
    weight: float = 0.0

    @property
    def is_empty(self) -> bool:
        return len(self.tables) == 0


@dataclass
class DatabaseSlotPlan:
    db: str
    strategy: SlotStrategy
    slots: list[SlotAssignment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def slot_count(self) -> int:
        return len(self.slots)


# --------------------------------------------------------------------------------------
# Union-Find (for FK-affinity grouping)
# --------------------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: list[TableRef]) -> None:
        self._parent: dict[TableRef, TableRef] = {i: i for i in items}

    def find(self, x: TableRef) -> TableRef:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # path compression
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: TableRef, b: TableRef) -> None:
        if a not in self._parent or b not in self._parent:
            return
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> list[list[TableRef]]:
        out: dict[TableRef, list[TableRef]] = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return list(out.values())


@dataclass(frozen=True)
class _Group:
    tables: tuple[TableRef, ...]
    weight: float


def fk_affinity_groups(
    tables: list[TableInput], fk_edges: list[FkEdge]
) -> list[_Group]:
    """Group tables into FK-connected components, summing weights (FR-11)."""
    weights = {t.ref: t.weight for t in tables}
    uf = _UnionFind([t.ref for t in tables])
    for a, b in fk_edges:
        uf.union(a, b)
    groups: list[_Group] = []
    for members in uf.groups():
        members_sorted = tuple(sorted(members))
        groups.append(_Group(members_sorted, sum(weights.get(m, 0.0) for m in members_sorted)))
    # deterministic ordering: heaviest first, then by first table name
    groups.sort(key=lambda g: (-g.weight, g.tables[0]))
    return groups


def lpt_pack(groups: list[_Group], n: int) -> list[list[_Group]]:
    """Greedy longest-processing-time bin-packing into ``n`` bins (FR-11)."""
    bins: list[list[_Group]] = [[] for _ in range(n)]
    loads = [0.0] * n
    for g in sorted(groups, key=lambda g: (-g.weight, g.tables[0])):
        i = min(range(n), key=lambda j: (loads[j], j))
        bins[i].append(g)
        loads[i] += g.weight
    return bins


# --------------------------------------------------------------------------------------
# Cross-slot FK detection (warning surface, FR-13/FR-14)
# --------------------------------------------------------------------------------------

def cross_slot_fk_warnings(
    slots: list[SlotAssignment], fk_edges: list[FkEdge]
) -> list[str]:
    slot_of: dict[TableRef, int] = {}
    for s in slots:
        for t in s.tables:
            slot_of[t] = s.index
    warnings: list[str] = []
    seen: set[tuple[int, int]] = set()
    for a, b in fk_edges:
        sa, sb = slot_of.get(a), slot_of.get(b)
        if sa is not None and sb is not None and sa != sb:
            key = (min(sa, sb), max(sa, sb))
            if key not in seen:
                seen.add(key)
                warnings.append(
                    f"cross-slot FK: {a.qualified} (slot {sa}) <-> {b.qualified} "
                    f"(slot {sb}); consider keeping FK-related tables in one slot"
                )
    return warnings


# --------------------------------------------------------------------------------------
# Strategy entry point
# --------------------------------------------------------------------------------------

def _expand_partitions(tables: list[TableInput]) -> list[TableInput]:
    """Replace partitioned parents with their partition children as separate units.

    Uses per-leaf weights when the caller supplied them (``partition_weights``);
    otherwise splits the parent's weight evenly across its leaves.
    """
    out: list[TableInput] = []
    for t in tables:
        if not t.is_partitioned:
            out.append(t)
            continue
        n = len(t.partitions)
        if t.partition_weights and len(t.partition_weights) == n:
            weights = t.partition_weights
        else:
            share = t.weight / n if n else t.weight
            weights = tuple(share for _ in range(n))
        out.extend(
            TableInput(ref=p, weight=w)
            for p, w in zip(t.partitions, weights, strict=True)
        )
    return out


def _mk_slots(bins: list[list[_Group]]) -> list[SlotAssignment]:
    slots: list[SlotAssignment] = []
    for idx, groups in enumerate(bins):
        tables = tuple(sorted(t for g in groups for t in g.tables))
        weight = sum(g.weight for g in groups)
        slots.append(SlotAssignment(index=idx, tables=tables, weight=weight))
    return slots


def plan_database_slots(
    db: str,
    tables: list[TableInput],
    fk_edges: list[FkEdge],
    cfg: SlotConfig,
    *,
    manual_map: dict[str, list[str]] | None = None,
) -> DatabaseSlotPlan:
    """Compute the slot layout for one database (FR-10..13).

    manual_map: for MANUAL strategy, {slot_name: [table patterns]} already resolved
    to concrete table lists is not required — this accepts explicit table lists.
    """
    plan = DatabaseSlotPlan(db=db, strategy=cfg.strategy)

    if cfg.spread_partitions:
        tables = _expand_partitions(tables)

    all_refs = sorted(t.ref for t in tables)

    if cfg.strategy == SlotStrategy.SINGLE:
        weight = sum(t.weight for t in tables)
        plan.slots = [SlotAssignment(0, tuple(all_refs), weight)]

    elif cfg.strategy == SlotStrategy.PER_SCHEMA:
        by_schema: dict[str, list[TableInput]] = {}
        for t in tables:
            by_schema.setdefault(t.ref.schema, []).append(t)
        for idx, schema in enumerate(sorted(by_schema)):
            members = by_schema[schema]
            plan.slots.append(
                SlotAssignment(
                    idx,
                    tuple(sorted(t.ref for t in members)),
                    sum(t.weight for t in members),
                )
            )

    elif cfg.strategy == SlotStrategy.BALANCED:
        if cfg.n > cfg.max_slots:
            raise SlotCapExceeded(
                f"balanced n={cfg.n} exceeds max_slots={cfg.max_slots} (decode-cost cap, FR-13)",
                hint="lower --n or raise --max-slots after considering publisher decode cost",
            )
        groups = fk_affinity_groups(tables, fk_edges)
        n = min(cfg.n, max(1, len(groups)))  # don't create empty bins beyond #groups
        plan.slots = _mk_slots(lpt_pack(groups, n))

    elif cfg.strategy == SlotStrategy.MANUAL:
        if manual_map is None:
            raise ConfigError("manual strategy requires a resolved slot map (FR-12)")
        plan.slots, warns = _plan_manual(all_refs, manual_map)
        plan.warnings.extend(warns)

    else:  # pragma: no cover - defensive
        raise ConfigError(f"unknown slot strategy: {cfg.strategy}")

    if len(plan.slots) > cfg.max_slots:
        raise SlotCapExceeded(
            f"{db}: computed {len(plan.slots)} slots exceeds max_slots={cfg.max_slots} "
            "(decode-cost cap, FR-13)"
        )

    plan.warnings.extend(cross_slot_fk_warnings(plan.slots, fk_edges))
    return plan


def _plan_manual(
    all_refs: list[TableRef], manual_map: dict[str, list[str]]
) -> tuple[list[SlotAssignment], list[str]]:
    """Resolve an explicit slot->table mapping, validating coverage (FR-12).

    Precedence: explicit table name > glob (e.g. ``audit.*``) > catch-all (``*``). A
    table matched explicitly by more than one slot, or by globs in more than one slot,
    is flagged ambiguous.
    """
    from fnmatch import fnmatch

    warnings: list[str] = []
    ref_set = set(all_refs)

    explicit: dict[str, set[TableRef]] = {}
    globs: dict[str, list[str]] = {}
    catch_all_slot: str | None = None

    for slot_name, patterns in manual_map.items():
        explicit.setdefault(slot_name, set())
        globs.setdefault(slot_name, [])
        for pat in patterns:
            if pat == "*":
                catch_all_slot = slot_name
            elif any(ch in pat for ch in "*?["):
                globs[slot_name].append(pat)
            else:
                explicit[slot_name].add(TableRef.parse(pat))

    assigned: dict[TableRef, str] = {}

    # 1) explicit table names (highest precedence)
    for slot_name, refs in explicit.items():
        for r in refs:
            if r in assigned:
                warnings.append(
                    f"ambiguous: {r.qualified} listed in both '{assigned[r]}' and '{slot_name}'"
                )
            assigned[r] = slot_name
            if r not in ref_set:
                warnings.append(f"not in scope / does not exist: {r.qualified}")

    # 2) globs (only for tables not already claimed by an explicit entry)
    for r in all_refs:
        if r in assigned:
            continue
        matches = [
            slot for slot, pats in globs.items()
            if any(fnmatch(r.qualified, p) for p in pats)
        ]
        if len(matches) > 1:
            warnings.append(
                f"ambiguous: {r.qualified} matched by glob(s) in {sorted(matches)}"
            )
        if matches:
            assigned[r] = matches[0]

    # 3) catch-all / leftover
    unassigned = [r for r in all_refs if r not in assigned]
    if catch_all_slot is not None:
        for r in unassigned:
            assigned[r] = catch_all_slot
    elif unassigned:
        raise ConfigError(
            "manual slot map does not cover all in-scope tables and has no catch-all "
            f"('*'); unassigned: {', '.join(r.qualified for r in unassigned)} (FR-12)"
        )

    slots: list[SlotAssignment] = []
    for idx, slot_name in enumerate(manual_map.keys()):
        refs = {r for r, s in assigned.items() if s == slot_name}
        if not refs:
            warnings.append(f"empty slot: '{slot_name}'")
        slots.append(SlotAssignment(idx, tuple(sorted(refs))))
    return slots, warnings
