"""Unit tests for REVIEW fixes: conninfo escaping (H1), publish allowlist (M4)."""

from __future__ import annotations

import pytest
from psycopg.conninfo import conninfo_to_dict

from pgreplkit.config.models import Endpoint, SlotConfig


def test_conninfo_escapes_password_with_spaces_and_quotes() -> None:
    ep = Endpoint(host="db.example.com", port=5432, user="postgres",
                  password="p ass'w0rd\\x", dbname="appdb")
    dsn = ep.dsn()
    # round-trips through libpq parsing to the exact original password (H1)
    parsed = conninfo_to_dict(dsn)
    assert parsed["password"] == "p ass'w0rd\\x"
    assert parsed["host"] == "db.example.com"
    assert parsed["dbname"] == "appdb"


def test_subscriber_dsn_uses_advertised_and_escapes() -> None:
    ep = Endpoint(host="pub.example.com", port=5432, user="u", password="a b c",
                  dbname="d", advertised_host="10.0.0.5", advertised_port=6543)
    parsed = conninfo_to_dict(ep.dsn_for_subscriber())
    assert parsed["host"] == "10.0.0.5"
    assert parsed["port"] == "6543"
    assert parsed["password"] == "a b c"


def test_publish_allowlist_rejects_bad_op() -> None:
    with pytest.raises(ValueError):
        SlotConfig(publish=["insert", "drop; DROP TABLE x"])


def test_publish_allowlist_accepts_valid() -> None:
    cfg = SlotConfig(publish=["insert", "update"])
    assert cfg.publish == ["insert", "update"]


def test_publish_empty_rejected() -> None:
    with pytest.raises(ValueError):
        SlotConfig(publish=[])
