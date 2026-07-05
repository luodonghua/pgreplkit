"""Glob-style include/exclude matching for scope filters (FR-8)."""

from __future__ import annotations

from fnmatch import fnmatch


def matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch(name, p) for p in patterns)


def in_scope(name: str, include: list[str], exclude: list[str]) -> bool:
    """True if ``name`` matches an include pattern and no exclude pattern."""
    included = matches_any(name, include) if include else True
    return included and not matches_any(name, exclude)
