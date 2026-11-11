"""Tests for integer-paise money helpers (sellershield_agent.money)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sellershield_agent.money import allocate_largest_remainder, format_inr, parse_rupees

# ── format_inr ────────────────────────────────────────────────────


def test_format_inr_whole_rupees() -> None:
    assert format_inr(120000) == "₹1,200"


def test_format_inr_fractional() -> None:
    assert format_inr(59899) == "₹598.99"


def test_format_inr_zero() -> None:
    assert format_inr(0) == "₹0"


def test_format_inr_single_paisa() -> None:
    assert format_inr(1) == "₹0.01"


def test_format_inr_ten_paise() -> None:
    assert format_inr(10) == "₹0.10"


def test_format_inr_rejects_negative() -> None:
    with pytest.raises(ValueError):
        format_inr(-1)


def test_format_inr_rejects_float() -> None:
    with pytest.raises(TypeError):
        format_inr(10.5)  # type: ignore[arg-type]


# ── parse_rupees ──────────────────────────────────────────────────


def test_parse_rupees_basic() -> None:
    assert parse_rupees("598.99") == 59899


def test_parse_rupees_whole() -> None:
    assert parse_rupees("1200") == 120000


def test_parse_rupees_rejects_float() -> None:
    with pytest.raises(TypeError):
        parse_rupees(1.5)  # type: ignore[arg-type]


def test_parse_rupees_rejects_negative() -> None:
    with pytest.raises(ValueError):
        parse_rupees("-10")


def test_parse_rupees_rejects_excess_decimals() -> None:
    with pytest.raises(ValueError):
        parse_rupees("100.999")


# ── allocate_largest_remainder ────────────────────────────────────


def test_allocate_exact_case() -> None:
    result = allocate_largest_remainder(10000, [139800, 34900])
    assert result == [8002, 1998]


@given(
    total=st.integers(0, 10_000_000),
    weights=st.lists(st.integers(1, 1_000_000), min_size=1, max_size=10),
)
@settings(deadline=None)
def test_allocate_sums_exactly_hypothesis(total: int, weights: list[int]) -> None:
    result = allocate_largest_remainder(total, weights)
    assert sum(result) == total
    assert len(result) == len(weights)
    assert all(x >= 0 for x in result)


def test_allocate_rejects_negative_total() -> None:
    with pytest.raises(ValueError):
        allocate_largest_remainder(-1, [1, 1])


def test_allocate_rejects_non_int_total() -> None:
    with pytest.raises(TypeError):
        allocate_largest_remainder(10.0, [1, 1])  # type: ignore[arg-type]


def test_allocate_rejects_empty_weights() -> None:
    with pytest.raises(ValueError):
        allocate_largest_remainder(100, [])


def test_allocate_rejects_all_zero_weights() -> None:
    with pytest.raises(ValueError):
        allocate_largest_remainder(100, [0, 0])


def test_allocate_rejects_negative_weight() -> None:
    with pytest.raises(ValueError):
        allocate_largest_remainder(100, [1, -1])


def test_allocate_huge_paise() -> None:
    result = allocate_largest_remainder(9_999_999_999, [1, 1])
    assert result == [5_000_000_000, 4_999_999_999]
    assert sum(result) == 9_999_999_999
