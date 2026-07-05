"""Check result model shared by preflight/validate (ok / warn / block)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Level(IntEnum):
    OK = 0
    WARN = 1
    BLOCK = 2

    @property
    def label(self) -> str:
        return {Level.OK: "ok", Level.WARN: "warn", Level.BLOCK: "block"}[self]


@dataclass(frozen=True)
class CheckResult:
    level: Level
    code: str
    message: str
    remediation: str | None = None
    subject: str | None = None  # e.g. db or table the result concerns


@dataclass
class CheckReport:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        self.results.append(result)

    def extend(self, results: list[CheckResult]) -> None:
        self.results.extend(results)

    @property
    def blocks(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == Level.BLOCK]

    @property
    def warns(self) -> list[CheckResult]:
        return [r for r in self.results if r.level == Level.WARN]

    @property
    def has_blocks(self) -> bool:
        return any(r.level == Level.BLOCK for r in self.results)

    @property
    def worst(self) -> Level:
        return max((r.level for r in self.results), default=Level.OK)
