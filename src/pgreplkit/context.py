"""Runtime context passed to phases: config, execution mode, logger, output options."""

from __future__ import annotations

from dataclasses import dataclass

from pgreplkit.config.models import Config, ExecutionMode
from pgreplkit.logconf import get_logger


@dataclass
class Context:
    config: Config
    mode: ExecutionMode = ExecutionMode.EXECUTE
    json_output: bool = False
    assume_yes: bool = False
    verbose: int = 0

    @property
    def log(self):  # noqa: ANN201 - logging.Logger
        return get_logger()

    @property
    def dry_run(self) -> bool:
        return self.mode is ExecutionMode.DRY_RUN

    @property
    def generate_only(self) -> bool:
        return self.mode is ExecutionMode.GENERATE_ONLY
