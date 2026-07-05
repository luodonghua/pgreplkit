"""discover phase: enumerate databases/schemas/tables; show in-scope vs skipped
(FR-4..9). Strictly read-only (FR-31).
"""

from __future__ import annotations

from pgreplkit.context import Context
from pgreplkit.core.topology import TopologyReport, discover_topology
from pgreplkit.report.render import render_topology


def run_discover(ctx: Context) -> TopologyReport:
    report = discover_topology(ctx.config.source, ctx.config.scope)
    render_topology(report, json_output=ctx.json_output)
    return report
