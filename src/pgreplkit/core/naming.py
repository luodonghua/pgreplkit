"""Object naming convention (FR-43).

Names are deterministic and discoverable so status/teardown can find them, and are
sanitized to valid, <=63-char identifiers (also valid as replication slot names,
which must match [a-z0-9_]).
"""

from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_IDENT = 63


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_")


def object_base(prefix: str, run_id: str, db: str, index: int) -> str:
    """Base name shared by the publication/subscription/slot of one slot.

    Publications, subscriptions and replication slots live in different namespaces,
    so a single base name per (db, index) is unambiguous.
    """
    name = f"{_slug(prefix)}_{_slug(run_id)}_{_slug(db)}_{index}"
    if len(name) <= _MAX_IDENT:
        return name
    # too long: keep prefix/run/index, truncate the db slug
    fixed = f"{_slug(prefix)}_{_slug(run_id)}__{index}"
    budget = _MAX_IDENT - len(fixed)
    return f"{_slug(prefix)}_{_slug(run_id)}_{_slug(db)[:max(1, budget)]}_{index}"


def is_managed_name(prefix: str, name: str) -> bool:
    """True if a name looks like one pgreplkit created (used by teardown safety)."""
    return name.startswith(f"{_slug(prefix)}_")
