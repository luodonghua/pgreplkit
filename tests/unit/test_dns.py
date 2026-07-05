"""Unit tests for the client DNS TTL resolver."""

from __future__ import annotations

from pgreplkit.core import dns


def test_managed_host_detection() -> None:
    assert dns.is_managed_host("mma.cluster-x.us-east-1.rds.amazonaws.com")
    assert dns.is_managed_host("db.abc.us-east-1.rds.amazonaws.com")
    assert not dns.is_managed_host("blue.internal.example.com")
    assert not dns.is_managed_host("127.0.0.1")


def test_ttl_selection() -> None:
    assert dns.ttl_for("x.rds.amazonaws.com") == dns.MANAGED_TTL == 1
    assert dns.ttl_for("blue.example.com") == dns.DEFAULT_TTL == 5
    assert dns.ttl_for("x.rds.amazonaws.com", override=9) == 9


def test_ip_literal_passthrough() -> None:
    # IP literals are not resolved (returns None -> caller uses host as-is)
    assert dns.resolve("10.1.3.130") is None
    assert dns.resolve("127.0.0.1") is None


def test_ttl_cache_and_expiry() -> None:
    dns.clear_cache()
    calls = {"n": 0}

    # monkeypatch getaddrinfo via a fake clock and a stub
    import socket

    orig = socket.getaddrinfo

    def fake_getaddrinfo(host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.5", 0))]

    socket.getaddrinfo = fake_getaddrinfo
    try:
        t = [1000.0]
        clock = lambda: t[0]  # noqa: E731
        # first resolve populates cache (managed -> ttl 1)
        assert dns.resolve("x.rds.amazonaws.com", _now=clock) == "192.0.2.5"
        assert calls["n"] == 1
        # within TTL -> cached, no new lookup
        t[0] += 0.5
        assert dns.resolve("x.rds.amazonaws.com", _now=clock) == "192.0.2.5"
        assert calls["n"] == 1
        # past TTL (1s) -> re-resolve
        t[0] += 1.0
        assert dns.resolve("x.rds.amazonaws.com", _now=clock) == "192.0.2.5"
        assert calls["n"] == 2
    finally:
        socket.getaddrinfo = orig
        dns.clear_cache()
