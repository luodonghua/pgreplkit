"""Integration test fixtures. Require a live PostgreSQL.

Set connection via env (skips the whole module if unset):
  PGREPLKIT_IT_HOST, PGREPLKIT_IT_PORT, PGREPLKIT_IT_USER, PGREPLKIT_IT_PASSWORD
"""

from __future__ import annotations

import os

import pytest

from pgreplkit.config.models import Endpoint

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def source_endpoint() -> Endpoint:
    host = os.environ.get("PGREPLKIT_IT_HOST")
    if not host:
        pytest.skip("PGREPLKIT_IT_HOST not set; skipping integration tests")
    return Endpoint(
        host=host,
        port=int(os.environ.get("PGREPLKIT_IT_PORT", "5432")),
        user=os.environ.get("PGREPLKIT_IT_USER", "postgres"),
        password=os.environ.get("PGREPLKIT_IT_PASSWORD"),
        dbname="postgres",
        advertised_host=os.environ.get("PGREPLKIT_IT_ADVERTISED_HOST"),
        advertised_port=int(p) if (p := os.environ.get("PGREPLKIT_IT_ADVERTISED_PORT")) else None,
    )


@pytest.fixture(scope="session")
def target_endpoint() -> Endpoint:
    host = os.environ.get("PGREPLKIT_IT_TGT_HOST")
    if not host:
        pytest.skip("PGREPLKIT_IT_TGT_HOST not set; skipping setup integration tests")
    return Endpoint(
        host=host,
        port=int(os.environ.get("PGREPLKIT_IT_TGT_PORT", "5432")),
        user=os.environ.get("PGREPLKIT_IT_TGT_USER", "postgres"),
        password=os.environ.get("PGREPLKIT_IT_TGT_PASSWORD"),
        dbname="postgres",
    )
