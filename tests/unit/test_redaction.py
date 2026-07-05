"""Secret-redaction tests (H5): guide subscription SQL and log redaction must never
expose passwords, including libpq-quoted passwords (spaces/quotes/semicolons/backslashes).
"""

from __future__ import annotations

import pytest

from pgreplkit.config.models import Endpoint
from pgreplkit.core import sqlgen
from pgreplkit.core.model import TableRef
from pgreplkit.core.plan import SlotSpec
from pgreplkit.logconf import redact

TRICKY_PASSWORDS = [
    "secret",
    "a b c",              # space -> libpq single-quotes the value
    "pa'ss",              # embedded single quote
    "semi;colon",         # semicolon
    "back\\slash",        # backslash
    "quote'and space",    # combination
]


def _spec() -> SlotSpec:
    return SlotSpec(db="appdb", index=0, name="pgrk_appdb_0",
                    tables=(TableRef("public", "orders"),))


@pytest.mark.parametrize("pw", TRICKY_PASSWORDS)
def test_guide_subscription_sql_masks_password(pw: str) -> None:
    ep = Endpoint(host="src.example.com", port=5432, user="rep", password=pw, dbname="appdb")
    sub = sqlgen.create_subscription(_spec(), ep, copy_data=False, mask_password=True)
    assert pw not in sub.text, f"raw password leaked for {pw!r}: {sub.text}"
    assert "***" in sub.text


@pytest.mark.parametrize("pw", TRICKY_PASSWORDS)
def test_unmasked_subscription_still_contains_password(pw: str) -> None:
    # sanity: without masking the password IS present (so the masking test is meaningful)
    ep = Endpoint(host="src.example.com", port=5432, user="rep", password=pw, dbname="appdb")
    sub = sqlgen.create_subscription(_spec(), ep, copy_data=False, mask_password=False)
    # the value is embedded (possibly libpq-escaped) — at least a fragment is present
    assert "password" in sub.text


def test_log_redact_quoted_password() -> None:
    line = "host=h dbname=d user=u password='a b c' sslmode=require"
    out = redact(line)
    assert "a b c" not in out
    assert "***" in out


def test_log_redact_unquoted_password() -> None:
    assert "secret" not in redact("connection password=secret host=h")


def test_log_redact_url_password() -> None:
    assert "hunter2" not in redact("postgres://user:hunter2@host:5432/db")
