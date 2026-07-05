"""Local manifest of created objects (FR-74) — the source of truth for status /
resume / reverse / teardown.

Written atomically (temp file + os.replace). A single owner performs all writes; under
concurrency, worker threads must funnel updates to the owner (DESIGN §7). In this
implementation setup writes the manifest from the orchestrating thread only.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_manifest_path(project: str) -> Path:
    base = Path(os.environ.get("PGREPLKIT_HOME", Path.home() / ".pgreplkit"))
    return base / f"{project}.json"


@dataclass
class SlotRecord:
    db: str
    index: int
    name: str
    tables: list[str]
    publish: list[str]
    via_partition_root: bool = False
    init_sync: str = "copy"
    seed_lsn: str | None = None
    state: str = "created"  # created | copying | streaming | error | torn_down


@dataclass
class Manifest:
    project: str
    run_id: str
    source: str
    target: str | None
    direction: str = "forward"  # forward | reverse
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    slots: list[SlotRecord] = field(default_factory=list)
    # preflight blocks explicitly overridden at setup time (--force / --force-correctness).
    overrides: list[dict] = field(default_factory=list)

    # --- persistence -----------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> Manifest | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        slots = [SlotRecord(**s) for s in data.pop("slots", [])]
        return cls(**data, slots=slots)

    def save(self, path: Path) -> None:
        self.updated_at = _now()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._to_dict(), indent=2))
        os.replace(tmp, path)  # atomic

    def _to_dict(self) -> dict:
        d = asdict(self)
        return d

    # --- mutation --------------------------------------------------------------------

    def upsert_slot(self, rec: SlotRecord) -> None:
        for i, existing in enumerate(self.slots):
            if (existing.db, existing.index) == (rec.db, rec.index):
                self.slots[i] = rec
                return
        self.slots.append(rec)

    def find_slot(self, db: str, index: int) -> SlotRecord | None:
        for s in self.slots:
            if (s.db, s.index) == (db, index):
                return s
        return None


def effective_endpoints(cfg, manifest: Manifest):
    """Return the (source, target) endpoints for the manifest's current direction.

    After `reverse`, the manifest direction is 'reverse' and the roles are swapped
    relative to the config (which still names the original blue as source). Status,
    validate, ready, and teardown must operate on the swapped endpoints (FR-73).
    """
    if manifest.direction == "reverse":
        return cfg.target, cfg.source
    return cfg.source, cfg.target
