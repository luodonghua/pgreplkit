"""Unit tests for ${VAR} environment interpolation in the config loader (M8)."""

from __future__ import annotations

import pytest

from pgreplkit.config.loader import load_config
from pgreplkit.errors import ConfigError


def _write(tmp_path, text):
    p = tmp_path / "config.yml"
    p.write_text(text)
    return p


def test_env_interpolation_expands_password(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_PGPASSWORD", "s3cr3t-value")
    p = _write(tmp_path, """
source:
  host: db.example.com
  user: rep
  password: ${TEST_PGPASSWORD}
  dbname: appdb
""")
    cfg = load_config(p)
    assert cfg.source.password.get_secret_value() == "s3cr3t-value"


def test_missing_env_var_is_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
    p = _write(tmp_path, """
source:
  host: db.example.com
  user: rep
  password: ${DEFINITELY_UNSET_VAR}
""")
    with pytest.raises(ConfigError):
        load_config(p)


def test_no_placeholder_is_left_verbatim(tmp_path) -> None:
    p = _write(tmp_path, """
source:
  host: db.example.com
  user: rep
  password: plain-literal
""")
    cfg = load_config(p)
    assert cfg.source.password.get_secret_value() == "plain-literal"
