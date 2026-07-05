"""Config loading from YAML and environment (FR-1..3).

Precedence: explicit YAML file > environment variables. Passwords may come from the
env (``PGPASSWORD``) or the YAML; they are stored as ``SecretStr`` and never logged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from pgreplkit.config.models import Config, Endpoint
from pgreplkit.errors import ConfigError

_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` references in string values using the environment.

    Playbook configs use e.g. ``password: ${PGPASSWORD}``; without expansion the literal
    string would be used as the password (M8). A referenced-but-unset variable is an
    error rather than a silent literal.
    """
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(
                    f"config references environment variable ${{{name}}} which is not set"
                )
            return os.environ[name]

        return _ENV_VAR.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def _endpoint_from_env(prefix: str) -> Endpoint | None:
    """Build an endpoint from PGREPLKIT_<PREFIX>_* env vars, if host is present."""
    host = os.environ.get(f"PGREPLKIT_{prefix}_HOST")
    if not host:
        return None
    return Endpoint(
        host=host,
        port=int(os.environ.get(f"PGREPLKIT_{prefix}_PORT", "5432")),
        user=os.environ.get(f"PGREPLKIT_{prefix}_USER", os.environ.get("PGUSER", "postgres")),
        password=os.environ.get(f"PGREPLKIT_{prefix}_PASSWORD", os.environ.get("PGPASSWORD")),
        dbname=os.environ.get(f"PGREPLKIT_{prefix}_DBNAME", "postgres"),
        sslmode=os.environ.get(f"PGREPLKIT_{prefix}_SSLMODE"),
    )


def load_config(path: Path | None) -> Config:
    """Load a :class:`Config` from a YAML file, or from the environment if no path."""
    if path is not None:
        cfg = _load_from_yaml(path)
    else:
        source = _endpoint_from_env("SOURCE")
        if source is None:
            raise ConfigError(
                "no config provided",
                hint="pass --config FILE or set PGREPLKIT_SOURCE_HOST (and optionally "
                "PGREPLKIT_TARGET_HOST) environment variables",
            )
        cfg = Config(source=source, target=_endpoint_from_env("TARGET"))

    # Resolve secrets from AWS Secrets Manager where secret_ref is set (FR-2/3).
    _resolve_secret(cfg.source, cfg)
    if cfg.target is not None:
        _resolve_secret(cfg.target, cfg)
    return cfg


def _resolve_secret(endpoint, cfg: Config) -> None:
    """If the endpoint has a secret_ref and no explicit password, fetch the password
    from AWS Secrets Manager (FR-2). Accepts a JSON {username,password} secret or a
    plain-string secret. Never logs the value."""
    if not endpoint.secret_ref or endpoint.password is not None:
        return
    import json

    import boto3
    from pydantic import SecretStr

    aws = cfg.aws
    session = boto3.Session(
        profile_name=aws.profile if aws else None,
        region_name=aws.region if aws else None,
    )
    try:
        raw = session.client("secretsmanager").get_secret_value(
            SecretId=endpoint.secret_ref
        )["SecretString"]
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"could not resolve secret_ref '{endpoint.secret_ref}': {exc}"
        ) from exc
    try:
        password = json.loads(raw).get("password", raw)
    except (ValueError, AttributeError):
        password = raw
    endpoint.password = SecretStr(password)


def _load_from_yaml(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw: Any = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping in {path}")
    raw = _interpolate_env(raw)
    try:
        return Config.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc
