"""globals phase (FR-32..35): reproduce required roles and detect tablespaces.

Roles missing on the target are created with a strong random password (source
passwords cannot be recovered on managed engines); the generated passwords are written
to a protected credentials file for DBA reference and never logged (FR-33). Non-default
tablespaces are detected and reported (unsupported on RDS/Aurora — map to default).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from pgreplkit.config.models import ExecutionMode
from pgreplkit.context import Context
from pgreplkit.core import catalog
from pgreplkit.core.connection import connect
from pgreplkit.core.manifest import default_manifest_path
from pgreplkit.core.model import quote_ident
from pgreplkit.errors import ConfigError
from pgreplkit.logconf import get_logger

log = get_logger()


@dataclass
class GlobalsResult:
    missing_roles: list[str] = field(default_factory=list)
    created_roles: list[str] = field(default_factory=list)
    generated_password_roles: list[str] = field(default_factory=list)
    tablespaces: list[str] = field(default_factory=list)
    credentials_path: str | None = None


def _creds_path(project: str) -> Path:
    return default_manifest_path(project).with_name(f"{project}.roles.json")


def run_globals(ctx: Context) -> GlobalsResult:
    cfg = ctx.config
    if cfg.target is None:
        raise ConfigError("globals requires a target endpoint")

    result = GlobalsResult()
    entry = cfg.source.dbname or "postgres"

    with connect(cfg.source, entry, read_only=True) as sc:
        src_details = catalog.role_details(sc)
        tablespaces = catalog.non_default_tablespaces(sc)
    with connect(cfg.target, cfg.target.dbname or "postgres", read_only=True) as tc:
        tgt_roles = catalog.list_roles(tc)

    missing = sorted(set(src_details) - tgt_roles)
    result.missing_roles = missing
    result.tablespaces = sorted(tablespaces)

    if tablespaces:
        log.warning(
            "non-default tablespaces in use: %s — not replicated; on RDS/Aurora map "
            "objects to the default tablespace",
            sorted(tablespaces),
        )

    if not missing:
        return result

    generated: dict[str, str] = {}
    stmts: list[tuple[str, tuple]] = []
    for name in missing:
        d = src_details[name]
        pwd = secrets.token_urlsafe(24)
        generated[name] = pwd
        attrs = []
        attrs.append("LOGIN" if d["can_login"] else "NOLOGIN")
        if d["createdb"]:
            attrs.append("CREATEDB")
        if d["createrole"]:
            attrs.append("CREATEROLE")
        # password passed as a parameter-free literal (role DDL can't be parameterized)
        stmt = (
            f"CREATE ROLE {quote_ident(name)} WITH {' '.join(attrs)} "
            f"PASSWORD {_quote_literal(pwd)}"
        )
        stmts.append((stmt, ()))
        result.generated_password_roles.append(name)

    if ctx.mode is ExecutionMode.EXECUTE:
        with connect(cfg.target, cfg.target.dbname or "postgres") as tc:
            for stmt, _ in stmts:
                tc.execute(stmt)
                result.created_roles.append(stmt.split()[2].strip('"'))
        # record generated passwords to a protected file (0600), never to logs
        path = _creds_path(cfg.project_name())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(generated, indent=2))
        os.chmod(path, 0o600)
        result.credentials_path = str(path)
        log.warning(
            "created %d role(s) with generated passwords; recorded to %s (chmod 600) "
            "for DBA reference — rotate as needed",
            len(result.created_roles), path,
        )
    else:
        for stmt, _ in stmts:
            # redact the password in dry-run/guide output
            print("-- " + stmt.split(" PASSWORD ")[0] + " PASSWORD '<generated>';")

    return result


def _quote_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"
