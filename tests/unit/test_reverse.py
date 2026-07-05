"""Unit tests for reverse-direction endpoint swap (FR-70)."""

from __future__ import annotations

from pgreplkit.config.models import Config, Endpoint, InitSync
from pgreplkit.phases.reverse import replace_endpoints


def test_replace_endpoints_swaps_and_forces_none() -> None:
    cfg = Config(
        source=Endpoint(host="blue", port=5432, user="u"),
        target=Endpoint(host="green", port=5432, user="u"),
        init_sync=InitSync.COPY,
    )
    rev = replace_endpoints(cfg)
    assert rev.source.host == "green"
    assert rev.target.host == "blue"
    assert rev.init_sync == InitSync.NONE
    # original unchanged
    assert cfg.source.host == "blue"
    assert cfg.init_sync == InitSync.COPY


def test_effective_endpoints_honors_manifest_direction() -> None:
    from pgreplkit.core.manifest import Manifest, effective_endpoints

    cfg = Config(
        source=Endpoint(host="blue", port=5432, user="u"),
        target=Endpoint(host="green", port=5432, user="u"),
    )
    fwd = Manifest(project="p", run_id="r", source="blue:5432", target="green:5432",
                   direction="forward")
    s, t = effective_endpoints(cfg, fwd)
    assert (s.host, t.host) == ("blue", "green")

    rev = Manifest(project="p", run_id="r", source="blue:5432", target="green:5432",
                   direction="reverse")
    s, t = effective_endpoints(cfg, rev)
    assert (s.host, t.host) == ("green", "blue")  # swapped for reverse
