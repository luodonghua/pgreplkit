"""reverse phase (FR-70..73): flip host1->host2 into host2->host1 for rollback.

Reverse is performed from an in-sync state: the forward direction must be caught up
(zero lag) and is torn down before the reverse direction is established, avoiding a
replication loop (FR-72). The reverse subscription uses initial-sync `none` (no
re-copy) because the clusters are equivalent at the swap point (FR-70).
"""

from __future__ import annotations

from dataclasses import dataclass

from pgreplkit.config.models import InitSync
from pgreplkit.context import Context
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.errors import ConfigError, ConfirmationRequired, NotReady
from pgreplkit.logconf import get_logger
from pgreplkit.phases.ready import evaluate_ready
from pgreplkit.phases.status import gather_status

log = get_logger()


@dataclass
class ReverseResult:
    steps: list[str]


def run_reverse(
    ctx: Context, *, allow_nonzero_lag: bool = False, writes_stopped: bool | None = None,
    force: bool = False, force_correctness: bool = False,
) -> ReverseResult:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("reverse requires source and target endpoints")
    manifest = Manifest.load(default_manifest_path(cfg.project_name()))
    if manifest is None:
        raise ConfigError("no manifest found; nothing to reverse")
    if manifest.direction == "reverse":
        raise ConfigError("manifest is already in the reverse direction")

    steps: list[str] = []

    # 0) writes on the NEW source (old target/green) must be quiesced for the whole swap.
    # Otherwise a commit between the zero-lag check / forward-teardown and the new slot's
    # creation is neither seeded (init none) nor captured -> lost on rollback (REVIEW M2).
    stopped = writes_stopped if writes_stopped is not None else ctx.assume_yes
    if not stopped:
        raise ConfirmationRequired(
            "reverse requires writes on the NEW source (the promoted green) to be stopped "
            "for the whole swap — otherwise writes committed during the flip are lost",
            hint="quiesce writes on green, then re-run with --writes-stopped/--yes",
        )
    steps.append("quiesce: confirmed writes stopped on the new source for the swap")

    # 1) require the forward direction to be in-sync (zero lag) before swapping (FR-71)
    if not allow_nonzero_lag:
        rows = gather_status(ctx)
        result = evaluate_ready(rows, 0)
        if not result.passed:
            raise NotReady(
                "forward direction is not in-sync (zero lag) — reverse must swap from a "
                "consistent point: " + "; ".join(result.reasons)
            )
    steps.append("verified forward direction in-sync (zero lag)")

    # 2) enforce reverse-direction preflight BEFORE tearing anything down (FR-71).
    # The former target must be a valid publisher and the former source a valid
    # subscriber/target; running this before teardown means a blocking issue cannot
    # leave us with the forward direction already destroyed (H3).
    reversed_cfg = replace_endpoints(cfg)
    reverse_ctx = Context(
        config=reversed_cfg,
        mode=ctx.mode,
        json_output=ctx.json_output,
        assume_yes=ctx.assume_yes,
        verbose=ctx.verbose,
    )
    from pgreplkit.phases.preflight import run_preflight
    from pgreplkit.phases.setup import _gate_preflight, run_setup

    report = run_preflight(reverse_ctx)
    _gate_preflight(report, force=force, force_correctness=force_correctness)
    steps.append("verified reverse-direction preflight (no blocking issues)")

    # 3) tear down the forward direction first (loop avoidance, FR-72)
    from pgreplkit.phases.teardown import run_teardown

    run_teardown(ctx, confirm=True)
    steps.append("tore down forward direction")

    # 4) establish the reverse direction with swapped endpoints, init-sync none (FR-70).
    # force flags are passed through so the internal preflight (which we already ran and
    # gated above) does not block on the same, already-accepted issues.
    new_manifest = run_setup(reverse_ctx, force=True, force_correctness=True)
    new_manifest.direction = "reverse"
    new_manifest.save(default_manifest_path(reversed_cfg.project_name()))
    steps.append(
        f"established reverse direction (init-sync none): "
        f"{reversed_cfg.source.host} -> {reversed_cfg.target.host}"
    )
    for s in steps:
        log.info("reverse: %s", s)
    return ReverseResult(steps=steps)


def replace_endpoints(cfg):
    """Swap source/target and force init-sync none (in-sync swap, no re-copy)."""
    # advertised host/port must also swap so the new subscriber can reach the new source
    new = cfg.model_copy(deep=True)
    new.source, new.target = cfg.target, cfg.source
    new.init_sync = InitSync.NONE
    return new
