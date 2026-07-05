"""Typed exceptions and their stable process exit codes (NFR-6).

Exit codes are part of the CLI contract so `preflight`, `validate`, and `ready`
can gate CI/CD pipelines. Keep these stable.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable process exit codes (NFR-6)."""

    OK = 0
    # Generic / unexpected
    ERROR = 1
    USAGE = 2
    # Validation / gating failures (used by preflight/validate/ready)
    PREFLIGHT_BLOCKED = 10
    VALIDATION_FAILED = 11
    NOT_READY = 12
    # Environment / connectivity
    CONNECTION_FAILED = 20
    ENGINE_UNSUPPORTED = 21
    VERSION_UNSUPPORTED = 22
    PERMISSION_DENIED = 23
    # State / planning
    PLAN_CONFLICT = 30
    MANIFEST_CONFLICT = 31
    SLOT_CAP_EXCEEDED = 32
    # Safety
    CONFIRMATION_REQUIRED = 40


class PgreplkitError(Exception):
    """Base class for all pgreplkit errors. Carries an exit code."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UsageError(PgreplkitError):
    exit_code = ExitCode.USAGE


class ConfigError(UsageError):
    """Invalid or missing configuration."""


class ConnectionFailed(PgreplkitError):
    exit_code = ExitCode.CONNECTION_FAILED


class EngineUnsupported(PgreplkitError):
    exit_code = ExitCode.ENGINE_UNSUPPORTED


class VersionUnsupported(PgreplkitError):
    exit_code = ExitCode.VERSION_UNSUPPORTED


class PermissionDenied(PgreplkitError):
    exit_code = ExitCode.PERMISSION_DENIED


class PreflightBlocked(PgreplkitError):
    """One or more block-level preflight checks failed (FR-38)."""

    exit_code = ExitCode.PREFLIGHT_BLOCKED


class ValidationFailed(PgreplkitError):
    """A `validate` comparison failed (FR-62)."""

    exit_code = ExitCode.VALIDATION_FAILED


class NotReady(PgreplkitError):
    """The composite `ready` gate did not pass (FR-64)."""

    exit_code = ExitCode.NOT_READY


class PlanConflict(PgreplkitError):
    """An existing object diverges from the computed plan (FR-41)."""

    exit_code = ExitCode.PLAN_CONFLICT


class ManifestConflict(PgreplkitError):
    exit_code = ExitCode.MANIFEST_CONFLICT


class SlotCapExceeded(PgreplkitError):
    """Requested slot count exceeds the configured decode-cost cap (FR-13)."""

    exit_code = ExitCode.SLOT_CAP_EXCEEDED


class ConfirmationRequired(PgreplkitError):
    """A destructive / billable action needs explicit confirmation (NFR-3)."""

    exit_code = ExitCode.CONFIRMATION_REQUIRED
