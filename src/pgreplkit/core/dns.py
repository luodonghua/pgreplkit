"""Client-side DNS resolution with a short TTL cache.

Managed RDS/Aurora endpoints reuse DNS names and can hand back stale/rotated IPs, so we
resolve them with a very short TTL (1s) and re-resolve often; other hosts use 5s. The
resolved IP is passed to libpq as ``hostaddr`` while the original name is kept as
``host`` (for TLS SNI / cert verification), so we control the effective client DNS TTL
regardless of OS resolver caching.
"""

from __future__ import annotations

import socket
import time

MANAGED_TTL = 1      # seconds — RDS/Aurora endpoints
DEFAULT_TTL = 5      # seconds — everything else

_cache: dict[str, tuple[str, float]] = {}


def is_managed_host(host: str) -> bool:
    """True for Amazon RDS/Aurora endpoints (…rds.amazonaws.com)."""
    return "rds.amazonaws.com" in host.lower()


def ttl_for(host: str, override: int | None = None) -> int:
    if override is not None:
        return override
    return MANAGED_TTL if is_managed_host(host) else DEFAULT_TTL


def _is_ip_literal(host: str) -> bool:
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, host)
            return True
        except OSError:
            continue
    return False


def resolve(host: str, ttl: int | None = None, *, _now=time.monotonic) -> str | None:
    """Return an IP for ``host`` using a TTL cache. None if it's already an IP or
    cannot be resolved (caller then falls back to name-based connection)."""
    if _is_ip_literal(host):
        return None
    effective = ttl_for(host, ttl)
    now = _now()
    cached = _cache.get(host)
    if cached is not None and cached[1] > now:
        return cached[0]
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    if not infos:
        return None
    ip = infos[0][4][0]
    _cache[host] = (ip, now + effective)
    return ip


def clear_cache() -> None:
    _cache.clear()
