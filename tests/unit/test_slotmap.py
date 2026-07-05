"""Unit tests for the manual slot-map YAML loader (FR-12)."""

from __future__ import annotations

import pytest

from pgreplkit.config.slotmap import load_slot_map
from pgreplkit.errors import ConfigError


def _write(tmp_path, text):
    p = tmp_path / "slotmap.yml"
    p.write_text(text)
    return p


def test_load_valid_slot_map(tmp_path) -> None:
    p = _write(tmp_path, """
databases:
  appdb:
    slots:
      - name: hot
        tables: [public.orders, public.order_items]
      - name: rest
        tables: ["*"]
""")
    m = load_slot_map(p)
    assert m == {"appdb": {"hot": ["public.orders", "public.order_items"], "rest": ["*"]}}


def test_missing_file(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_slot_map(tmp_path / "nope.yml")


def test_missing_databases_key(tmp_path) -> None:
    p = _write(tmp_path, "foo: bar\n")
    with pytest.raises(ConfigError):
        load_slot_map(p)


def test_slot_missing_tables(tmp_path) -> None:
    p = _write(tmp_path, """
databases:
  appdb:
    slots:
      - name: hot
""")
    with pytest.raises(ConfigError):
        load_slot_map(p)


def test_empty_slots(tmp_path) -> None:
    p = _write(tmp_path, "databases:\n  appdb:\n    slots: []\n")
    with pytest.raises(ConfigError):
        load_slot_map(p)
