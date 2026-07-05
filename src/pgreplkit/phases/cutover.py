"""cutover phase (FR-65): ordered, safety-gated cutover orchestration.

Order: quiesce writes on source (operator-confirmed) -> drain until lag 0 ->
sync-sequences -> validate -> signal ready for the external traffic switch.
Refuses to advance past drain on nonzero lag, and refuses to signal on validation
failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pgreplkit.config.models import ValidateDepth
from pgreplkit.context import Context
from pgreplkit.errors import ConfirmationRequired, NotReady, ValidationFailed
from pgreplkit.logconf import get_logger
from pgreplkit.phases.ready import evaluate_ready
from pgreplkit.phases.sequences import run_sync_sequences
from pgreplkit.phases.status import gather_status
from pgreplkit.phases.validate import run_validate

log = get_logger()


@dataclass
class CutoverResult:
    signalled: bool
    steps: list[str]


def run_cutover(
    ctx: Context,
    *,
    writes_stopped: bool | None = None,
    drain_timeout_s: int = 120,
    validate_depth: ValidateDepth = ValidateDepth.SAMPLED,
) -> CutoverResult:
    steps: list[str] = []
    stopped = writes_stopped if writes_stopped is not None else ctx.assume_yes

    # 1) quiesce — the tool cannot stop the app; require confirmation writes have ceased
    if not stopped:
        raise ConfirmationRequired(
            "cutover requires writes on the source to be stopped first",
            hint="quiesce the application, then re-run with --writes-stopped/--yes",
        )
    steps.append("quiesce: confirmed writes stopped on source")

    # 2) drain until lag reaches zero across all slots
    deadline = time.time() + drain_timeout_s
    while True:
        rows = gather_status(ctx)
        result = evaluate_ready(rows, 0)  # require true zero lag at cutover
        if result.passed:
            break
        if time.time() >= deadline:
            raise NotReady(
                "drain did not reach zero lag within timeout: " + "; ".join(result.reasons)
            )
        time.sleep(2)
    steps.append("drain: all slots synced at zero lag")

    # 3) sync sequences (must be AFTER writes stopped)
    n = run_sync_sequences(ctx)
    steps.append(f"sequences: synced {n} sequence(s)")

    # 4) validate correctness
    report = run_validate(ctx, validate_depth)
    if report.has_blocks:
        raise ValidationFailed(
            "validation failed at cutover: "
            + "; ".join(r.message for r in report.blocks)
        )
    steps.append("validate: source and target match")

    # 5) signal ready for external traffic switch
    steps.append("READY FOR CUTOVER: switch application traffic to the target now")
    for s in steps:
        log.info("cutover: %s", s)
    return CutoverResult(signalled=True, steps=steps)
