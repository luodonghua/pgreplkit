"""Logging configuration with verbosity control and secret redaction (NFR-5, FR-2).

Named ``logconf`` rather than ``logging`` (as in DESIGN.md) to avoid shadowing the
stdlib ``logging`` module within the package.
"""

from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = [
    # password=... in a libpq/DSN connection string. Handles both a libpq
    # single-quoted value ('a b c', with '' escapes) and an unquoted token.
    re.compile(r"(password=)('(?:[^']|'')*'|[^\s;']+)", re.IGNORECASE),
    # postgres://user:pass@host  -> redact the pass
    re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)"),
]


class RedactingFilter(logging.Filter):
    """Redacts obvious secrets from log records (defense-in-depth, FR-2)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = tuple(
                redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def redact(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + "***" + (m.group(3) if m.lastindex == 3 else ""), out)
    return out


def configure(verbose: int = 0, json_mode: bool = False) -> logging.Logger:
    """Configure the root ``pgreplkit`` logger.

    verbose: 0 -> WARNING, 1 -> INFO, 2+ -> DEBUG.
    """
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG

    logger = logging.getLogger("pgreplkit")
    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    fmt = "%(levelname)s %(name)s: %(message)s" if not json_mode else "%(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger(name: str = "pgreplkit") -> logging.Logger:
    return logging.getLogger(name)
