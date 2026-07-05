"""plan phase (FR-12): compute the balanced slot layout and emit it as editable YAML,
which can be hand-tuned and fed back via `--slots manual --slot-map`.
"""

from __future__ import annotations

import yaml

from pgreplkit.context import Context
from pgreplkit.core.plan import build_cluster_plan


def run_plan(ctx: Context) -> dict:
    import secrets

    plan = build_cluster_plan(ctx.config, run_id=f"plan_{secrets.token_hex(2)}")
    doc: dict = {"databases": {}}
    for dbp in plan.databases:
        doc["databases"][dbp.db] = {
            "slots": [
                {"name": f"slot_{s.index}", "tables": [t.qualified for t in s.tables]}
                for s in dbp.slots
            ]
        }
        if dbp.warnings:
            doc["databases"][dbp.db]["warnings"] = dbp.warnings
    print(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return doc
