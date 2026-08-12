"""Settlement rules contract packaging / path integrity (DWCS-200)."""

from __future__ import annotations

from mma_model.markets.rules import (
    EXPECTED_CONTRACT_VERSION,
    package_settlement_resource_path,
    visible_settlement_path,
)


def test_visible_config_matches_packaged_bytes() -> None:
    packaged = package_settlement_resource_path()
    visible = visible_settlement_path()
    assert packaged.is_file()
    assert visible.is_file()
    assert packaged.read_bytes() == visible.read_bytes()
    assert EXPECTED_CONTRACT_VERSION == "1.0.0"
