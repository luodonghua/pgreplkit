"""apply-side operations (FR-58..60): skip a failing transaction on a subscription.

Skipping loses the data in that transaction, so it is confirmation-gated (NFR-3).
"""

from __future__ import annotations

from pgreplkit.config.models import ExecutionMode
from pgreplkit.context import Context
from pgreplkit.core import sqlgen
from pgreplkit.core.connection import connect
from pgreplkit.core.executor import Executor
from pgreplkit.core.manifest import Manifest, default_manifest_path
from pgreplkit.errors import ConfigError, ConfirmationRequired


def run_skip(ctx: Context, slot_name: str, lsn: str, *, confirm: bool | None = None) -> None:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("skip requires a target endpoint")
    manifest = Manifest.load(default_manifest_path(cfg.project_name()))
    if manifest is None:
        raise ConfigError("no manifest found; run setup first")

    rec = next((s for s in manifest.slots if s.name == slot_name), None)
    if rec is None:
        raise ConfigError(
            f"subscription '{slot_name}' not found in manifest",
            hint="see `pgreplkit status` for slot names",
        )

    proceed = confirm if confirm is not None else ctx.assume_yes
    if ctx.mode is ExecutionMode.EXECUTE and not proceed:
        raise ConfirmationRequired(
            f"skipping LSN {lsn} on {slot_name} permanently discards that transaction",
            hint="re-run with --yes to confirm",
        )

    executor = Executor(ctx.mode)
    from pgreplkit.core.manifest import effective_endpoints

    _src_ep, tgt_ep = effective_endpoints(cfg, manifest)
    with connect(tgt_ep, rec.db) as tconn:
        executor.run(tconn, sqlgen.skip_transaction(slot_name, lsn))
