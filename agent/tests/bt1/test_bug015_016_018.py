"""Regression tests for BUG-015, BUG-016, and BUG-018."""

from sellershield_agent.understand.router import (
    extract_order_ids,
    normalize_order_id,
)


def test_typo_order_id_matching() -> None:
    # BUG-015 typo regex test
    assert extract_order_ids("wher is ordr 1048 plz") == ["ORD-1048"]
    assert extract_order_ids("status for odr 1048") == ["ORD-1048"]
    assert normalize_order_id("ordr 1048") == "ORD-1048"
    assert normalize_order_id("odr 1048") == "ORD-1048"
