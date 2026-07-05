"""pgreplkit CLI (typer). Wires every command to its phase.

Global options select config, provisioning mode, scope filters, slot strategy,
init-sync, and the execution mode (execute / dry-run / generate-only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from pgreplkit import __version__
from pgreplkit.config.models import (
    ExecutionMode,
    InitSync,
    ProvisionMode,
    SlotStrategy,
    ValidateDepth,
)
from pgreplkit.errors import PgreplkitError
from pgreplkit.logconf import configure

app = typer.Typer(
    name="pgreplkit",
    help="Automate & de-risk PostgreSQL logical replication for blue-green "
    "deployments (RDS/Aurora aware).",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pgreplkit {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config YAML."),
    mode: ExecutionMode = typer.Option(
        ExecutionMode.EXECUTE, "--mode-exec", help="Execution mode."
    ),
    provision_mode: ProvisionMode = typer.Option(
        ProvisionMode.EXISTING, "--mode", help="Provisioning mode (existing|provision)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print SQL, change nothing."),
    generate_only: bool = typer.Option(
        False, "--generate-only", help="Emit a manual runbook; execute nothing."
    ),
    assume_yes: bool = typer.Option(False, "--yes", "-y", help="Assume yes to prompts."),
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity."),
    _version: bool = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """pgreplkit — see `pgreplkit COMMAND --help` for each phase."""
    configure(verbose=verbose, json_mode=json_output)
    resolved_mode = mode
    if generate_only:
        resolved_mode = ExecutionMode.GENERATE_ONLY
    elif dry_run:
        resolved_mode = ExecutionMode.DRY_RUN
    ctx.obj = {
        "config_path": config,
        "mode": resolved_mode,
        "provision_mode": provision_mode,
        "json": json_output,
        "assume_yes": assume_yes,
        "verbose": verbose,
    }


def _build_context(tctx: typer.Context) -> object:
    """Load config and assemble a runtime Context from the global options."""
    from pgreplkit.config.loader import load_config
    from pgreplkit.context import Context

    obj = tctx.obj or {}
    config = load_config(obj.get("config_path"))
    config.provision_mode = obj.get("provision_mode", config.provision_mode)
    return Context(
        config=config,
        mode=obj.get("mode"),
        json_output=obj.get("json", False),
        assume_yes=obj.get("assume_yes", False),
        verbose=obj.get("verbose", 0),
    )


# --- Read-only discovery / validation -------------------------------------------------

@app.command()
def discover(ctx: typer.Context) -> None:
    """Enumerate databases/schemas/tables; show in-scope vs skipped (FR-4..9)."""
    from pgreplkit.phases.discover import run_discover

    run_discover(_build_context(ctx))


@app.command()
def plan(
    ctx: typer.Context,
    slots: SlotStrategy = typer.Option(SlotStrategy.BALANCED, "--slots"),
    n: int = typer.Option(4, "--n"),
) -> None:
    """Compute & emit the balanced slot layout as editable YAML (FR-12)."""
    from pgreplkit.phases.plan import run_plan

    context = _build_context(ctx)
    context.config.slots.strategy = slots
    context.config.slots.n = n
    run_plan(context)


@app.command()
def preflight(ctx: typer.Context) -> None:
    """Read-only eligibility & prerequisite report, source + target (FR-14..31)."""
    from pgreplkit.errors import PreflightBlocked
    from pgreplkit.phases.preflight import run_preflight
    from pgreplkit.report.render import render_checks

    context = _build_context(ctx)
    report = run_preflight(context)
    render_checks(report, json_output=context.json_output)
    if report.has_blocks:
        raise PreflightBlocked(
            f"{len(report.blocks)} blocking issue(s) must be resolved before setup"
        )


# --- Setup / sync ---------------------------------------------------------------------

@app.command()
def globals(ctx: typer.Context) -> None:
    """Detect/recreate roles & tablespaces on target (FR-32..35)."""
    from pgreplkit.phases.globals_ import run_globals

    result = run_globals(_build_context(ctx))
    typer.secho(
        f"roles missing on target: {len(result.missing_roles)}; "
        f"created: {len(result.created_roles)}; "
        f"non-default tablespaces: {len(result.tablespaces)}.",
        fg=typer.colors.GREEN,
    )
    if result.credentials_path:
        typer.secho(
            f"generated role passwords recorded to {result.credentials_path} (chmod 600)",
            fg=typer.colors.YELLOW,
        )


@app.command()
def setup(
    ctx: typer.Context,
    init_sync: InitSync = typer.Option(InitSync.COPY, "--init-sync"),
    slots: SlotStrategy = typer.Option(SlotStrategy.BALANCED, "--slots"),
    n: int = typer.Option(4, "--n"),
    slot_map: Path | None = typer.Option(None, "--slot-map"),
    spread_partitions: bool = typer.Option(False, "--spread-partitions"),
    max_slots: int = typer.Option(8, "--max-slots"),
    force: bool = typer.Option(
        False, "--force",
        help="Override capacity/environment preflight blocks (not correctness).",
    ),
    force_correctness: bool = typer.Option(
        False, "--force-correctness",
        help="Also override CORRECTNESS/data-loss preflight blocks (replica identity, "
        "schema/column, encoding, version). Recorded in the manifest.",
    ),
    confirm_provision: bool = typer.Option(
        False, "--confirm-provision",
        help="Confirm billable AWS resource creation in provision mode.",
    ),
) -> None:
    """Create publications/subscriptions/slots (FR-36..45)."""
    from pgreplkit.phases.setup import run_setup

    context = _build_context(ctx)
    context.config.init_sync = init_sync
    context.config.slots.strategy = slots
    context.config.slots.n = n
    context.config.slots.max_slots = max_slots
    context.config.slots.spread_partitions = spread_partitions
    if slot_map is not None:
        context.config.slots.slot_map = slot_map
    manifest = run_setup(
        context,
        force=force,
        force_correctness=force_correctness,
        confirm_provision=confirm_provision or context.assume_yes,
    )
    typer.secho(
        f"setup complete: {len(manifest.slots)} slot(s) across "
        f"{len({s.db for s in manifest.slots})} database(s).",
        fg=typer.colors.GREEN,
    )


@app.command()
def refresh(ctx: typer.Context) -> None:
    """ALTER PUBLICATION ADD TABLE + REFRESH for newly appeared tables (FR-44)."""
    from pgreplkit.phases.refresh import run_refresh

    n = run_refresh(_build_context(ctx))
    typer.secho(f"added {n} new table(s) to replication.", fg=typer.colors.GREEN)


# --- Monitoring / validation ----------------------------------------------------------

@app.command()
def status(ctx: typer.Context) -> None:
    """Replication + initial-sync + slot/WAL health, per db/slot (FR-54..56)."""
    from pgreplkit.phases.status import run_status

    run_status(_build_context(ctx))


@app.command()
def watch(
    ctx: typer.Context,
    interval: int = typer.Option(5, "--interval", help="Refresh seconds."),
) -> None:
    """Continuous status until caught up / interrupted (FR-57)."""
    import time

    from pgreplkit.phases.ready import evaluate_ready
    from pgreplkit.phases.status import run_status

    context = _build_context(ctx)
    try:
        while True:
            rows = run_status(context)
            if evaluate_ready(rows, context.config.lag_threshold_bytes).passed:
                typer.secho("all slots caught up.", fg=typer.colors.GREEN)
                break
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover
        typer.echo("\ninterrupted.")


@app.command()
def validate(
    ctx: typer.Context,
    depth: ValidateDepth = typer.Option(ValidateDepth.SAMPLED, "--depth"),
) -> None:
    """Object/global counts + per-table row-count/checksum comparison (FR-61..62)."""
    from pgreplkit.phases.validate import run_validate_cli

    run_validate_cli(_build_context(ctx), depth)


@app.command()
def ready(ctx: typer.Context) -> None:
    """Composite pass/fail gate: sync+errors+slot+lag (FR-64)."""
    from pgreplkit.errors import NotReady
    from pgreplkit.phases.ready import run_ready

    result = run_ready(_build_context(ctx))
    if not result.passed:
        raise NotReady("not ready for cutover")


# --- Cutover / rollback / teardown ----------------------------------------------------

@app.command()
def sync_sequences(ctx: typer.Context) -> None:
    """Copy sequence values source -> target (FR-63)."""
    from pgreplkit.phases.sequences import run_sync_sequences

    n = run_sync_sequences(_build_context(ctx))
    typer.secho(f"synced {n} sequence(s).", fg=typer.colors.GREEN)


@app.command()
def cutover(
    ctx: typer.Context,
    writes_stopped: bool = typer.Option(
        False, "--writes-stopped", help="Confirm writes on the source are stopped."
    ),
    drain_timeout: int = typer.Option(120, "--drain-timeout", help="Max seconds to drain."),
) -> None:
    """Ordered: quiesce -> drain -> sequences -> validate -> signal (FR-65)."""
    from pgreplkit.phases.cutover import run_cutover

    context = _build_context(ctx)
    result = run_cutover(
        context,
        writes_stopped=writes_stopped or context.assume_yes,
        drain_timeout_s=drain_timeout,
    )
    for s in result.steps:
        typer.secho(f"  ✓ {s}", fg=typer.colors.GREEN)


@app.command()
def reverse(
    ctx: typer.Context,
    writes_stopped: bool = typer.Option(
        False, "--writes-stopped", help="Confirm writes on the new source (green) are stopped."
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Override capacity/environment blocks in the reverse-direction preflight.",
    ),
    force_correctness: bool = typer.Option(
        False, "--force-correctness",
        help="Also override reverse-direction correctness/data-loss preflight blocks.",
    ),
) -> None:
    """Flip host1->host2 into host2->host1 for rollback (FR-70..73)."""
    from pgreplkit.phases.reverse import run_reverse

    context = _build_context(ctx)
    result = run_reverse(
        context,
        writes_stopped=writes_stopped or context.assume_yes,
        force=force,
        force_correctness=force_correctness,
    )
    for s in result.steps:
        typer.secho(f"  ✓ {s}", fg=typer.colors.GREEN)


@app.command()
def skip(
    ctx: typer.Context,
    slot: str = typer.Option(..., "--slot", help="Subscription/slot name (see status)."),
    lsn: str = typer.Option(..., "--lsn", help="LSN of the failing transaction to skip."),
) -> None:
    """ALTER SUBSCRIPTION ... SKIP a failing txn (confirmation required) (FR-59)."""
    from pgreplkit.phases.apply_ops import run_skip

    run_skip(_build_context(ctx), slot, lsn)
    typer.secho(f"skip issued on {slot} (lsn={lsn}).", fg=typer.colors.YELLOW)


@app.command()
def teardown(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm destructive teardown."),
) -> None:
    """Remove created artifacts (confirmation required) (FR-66..69)."""
    from pgreplkit.phases.teardown import run_teardown

    context = _build_context(ctx)
    run_teardown(context, confirm=yes or context.assume_yes)
    typer.secho("teardown complete.", fg=typer.colors.GREEN)


@app.command()
def guide(
    ctx: typer.Context,
    init_sync: InitSync = typer.Option(InitSync.COPY, "--init-sync"),
    slots: SlotStrategy = typer.Option(SlotStrategy.BALANCED, "--slots"),
    n: int = typer.Option(4, "--n"),
) -> None:
    """Generate a manual runbook (SQL + AWS CLI), execute nothing (FR-75..80)."""
    from pgreplkit.config.models import ExecutionMode
    from pgreplkit.phases.guide import run_guide

    context = _build_context(ctx)
    context.mode = ExecutionMode.GENERATE_ONLY  # guide never executes (FR-76)
    context.config.init_sync = init_sync
    context.config.slots.strategy = slots
    context.config.slots.n = n
    run_guide(context)


def run() -> None:
    """Entry wrapper that maps PgreplkitError -> stable exit codes (NFR-6)."""
    try:
        app()
    except PgreplkitError as exc:  # pragma: no cover - exercised via CLI
        msg = exc.message
        if exc.hint:
            msg += f"\nhint: {exc.hint}"
        typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
        sys.exit(int(exc.exit_code))


if __name__ == "__main__":  # pragma: no cover
    run()
