"""Execution layer: one path for execute / dry-run / generate-only (DESIGN §5).

SQL is represented as data (:class:`Sql`) so the same object can be executed,
printed (dry-run), or recorded into a runbook (generate-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from pgreplkit.config.models import ExecutionMode
from pgreplkit.logconf import get_logger

log = get_logger()


@dataclass(frozen=True)
class Sql:
    text: str
    params: tuple = ()
    note: str | None = None
    # where the statement runs, for guide grouping: "source" | "target"
    target: str = "source"


@dataclass
class Executor:
    mode: ExecutionMode = ExecutionMode.EXECUTE
    recorded: list[Sql] = field(default_factory=list)

    def run(self, conn: psycopg.Connection | None, sql: Sql) -> None:
        if self.mode is ExecutionMode.EXECUTE:
            if conn is None:  # pragma: no cover - defensive
                raise RuntimeError("execute mode requires a live connection")
            log.info("executing: %s", sql.note or sql.text.split("\n", 1)[0])
            conn.execute(sql.text, sql.params or None)
        elif self.mode is ExecutionMode.DRY_RUN:
            self.recorded.append(sql)
            print(f"-- [dry-run] {sql.note or ''}\n{sql.text};")
        else:  # GENERATE_ONLY
            self.recorded.append(sql)
