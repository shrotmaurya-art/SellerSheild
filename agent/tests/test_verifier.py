"""Adversarial draft verification tests, templates verification, and property tests.

Spec: docs/spec/02-agent.md sections 4.5, 4.6 and 21 (amendments 2 and 6).
Invariants:
- AGENTS.md rule 2.8: Every customer-facing reply passes the verifier.
- KP-12: Verifier bypass is a bug.
- KP-05: Money as int paise, never float.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sellershield_agent.clock import FixedClock
from sellershield_agent.compose.factsheet import FactSheet, build_factsheet
from sellershield_agent.compose.regenerate import build_regeneration_prompt
from sellershield_agent.compose.templates import (
    delay_options,
    guided_mode_menu,
    human_handoff_unavailable,
    not_found,
    order_status,
    policy_summary,
    refund_status,
    return_eligibility_preview,
    shipping_status,
    store_unavailable,
)
from sellershield_agent.compose.verifier import verify
from sellershield_agent.domain.models import OrderStatus
from sellershield_agent.money import format_inr
from sellershield_agent.tools.base import ToolResult

T0_BASE = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
FIXTURES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "fixtures" / "store.json"
)


@pytest.fixture
def sample_factsheet() -> FactSheet:
    return FactSheet(
        allowed_ids=frozenset(
            {"ORD-1042", "RET-2001", "RFD-3001", "CASE-4001", "APR-5001", "PRP-6001"}
        ),
        amounts_paise=frozenset({120000, 59899, 43900, 0}),
        dates=frozenset({date(2026, 11, 8), date(2026, 11, 12)}),
        relative_facts={"eta_in_days": 4},
        supported_claims=frozenset({"shipped", "approved"}),
        promise_facts=frozenset(),
        rule_texts=("Return window is 7 days for electronics.",),
        language="en",
        order_id="ORD-1042",
        carrier="Bluedart",
        tracking_no="TRK-987654",
        status=OrderStatus.shipped.value,
    )


# ── 1. Amount Verification Tests ────────────────────────────────────


def test_invented_amount_fails(sample_factsheet: FactSheet) -> None:
    draft = "Your order ORD-1042 refund amount is ₹9,999."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("amount" in v.lower() for v in res.violations)


def test_amount_formatting_variants_correct_passes(sample_factsheet: FactSheet) -> None:
    # 120000 paise = ₹1,200
    valid_variants = [
        "Refund of ₹1,200 for ORD-1042.",
        "Refund of ₹1200 for ORD-1042.",
        "Refund of ₹ 1,200 for ORD-1042.",
        "Refund of Rs 1,200 for ORD-1042.",
        "Refund of Rs. 1200 for ORD-1042.",
        "Refund of 1,200 rupees for ORD-1042.",
        "Refund of 1200 rupaye for ORD-1042.",
        "Refund of INR 1,200 for ORD-1042.",
    ]
    for variant in valid_variants:
        res = verify(variant, sample_factsheet, language="en")
        assert res.ok, f"Failed on valid variant: {variant}, violations: {res.violations}"


def test_amount_formatting_variants_incorrect_fails(sample_factsheet: FactSheet) -> None:
    invalid_variants = [
        "Refund of ₹1,201 for ORD-1042.",
        "Refund of ₹1199 for ORD-1042.",
        "Refund of Rs 500 for ORD-1042.",
        "Refund of 1,250 rupees for ORD-1042.",
    ]
    for variant in invalid_variants:
        res = verify(variant, sample_factsheet, language="en")
        assert not res.ok, f"Should have failed on: {variant}"
        assert any("amount" in v.lower() for v in res.violations)


def test_spelled_out_amount_fails(sample_factsheet: FactSheet) -> None:
    spelled_out = [
        "You will receive twelve hundred rupees for ORD-1042.",
        "Total is one thousand rupees for ORD-1042.",
        "ORD-1042 refund is five hundred rs.",
        "Apko 5 sau rupaye milenge ORD-1042 ke liye.",
        "Do hazaar rupaye ORD-1042 ke bante hain.",
        "One lakh rupees refund for ORD-1042.",
    ]
    for s in spelled_out:
        res = verify(s, sample_factsheet, language="en")
        assert not res.ok, f"Should have failed spelled-out: {s}"
        assert any("spelled-out" in v.lower() or "amount" in v.lower() for v in res.violations)


# ── 2. Identifier Verification Tests ────────────────────────────────


def test_wrong_order_id_fails(sample_factsheet: FactSheet) -> None:
    draft = "Your order ORD-9999 has been shipped."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("ORD-9999" in v for v in res.violations)


def test_lowercase_order_id_verified(sample_factsheet: FactSheet) -> None:
    # ord-1042 should be matched case-insensitively and pass because ORD-1042 is allowed
    draft = "Your order ord-1042 has been shipped."
    res = verify(draft, sample_factsheet, language="en")
    assert res.ok, f"Failed with violations: {res.violations}"

    # ord-9999 should fail
    draft_bad = "Your order ord-9999 has been shipped."
    res_bad = verify(draft_bad, sample_factsheet, language="en")
    assert not res_bad.ok
    assert any("ORD-9999" in v.upper() for v in res_bad.violations)


def test_other_customer_id_fails(sample_factsheet: FactSheet) -> None:
    draft = "Customer C002, your order ORD-1042 is on the way."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("customer id" in v.lower() or "C002" in v for v in res.violations)


# ── 3. Date & Relative Temporal Verification Tests ──────────────────


def test_wrong_date_fails(sample_factsheet: FactSheet) -> None:
    draft = "Your order ORD-1042 will arrive on 25 December 2026."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("date" in v.lower() for v in res.violations)


def test_matching_date_passes(sample_factsheet: FactSheet) -> None:
    draft = "Your order ORD-1042 is expected on 12 Nov 2026."
    res = verify(draft, sample_factsheet, language="en")
    assert res.ok, f"Failed on valid date: {res.violations}"


def test_tomorrow_with_no_eta_fails(sample_factsheet: FactSheet) -> None:
    # relative_facts has eta_in_days = 4, not tomorrow (1)
    draft = "Your order ORD-1042 will arrive tomorrow."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("relative" in v.lower() or "tomorrow" in v.lower() for v in res.violations)


def test_valid_relative_days_passes(sample_factsheet: FactSheet) -> None:
    # eta_in_days = 4
    draft = "Your order ORD-1042 will arrive in 4 days."
    res = verify(draft, sample_factsheet, language="en")
    assert res.ok, f"Failed on valid relative days: {res.violations}"


# ── 4. Claim & Promise Word Tests ───────────────────────────────────


def test_claim_without_fact_fails(sample_factsheet: FactSheet) -> None:
    # supported_claims has 'shipped', 'approved' but NOT 'refunded' or 'delivered'
    draft1 = "Your refund has been processed for ORD-1042."
    res1 = verify(draft1, sample_factsheet, language="en")
    assert not res1.ok
    assert any("claim" in v.lower() or "processed" in v.lower() for v in res1.violations)

    draft2 = "Your package was delivered for ORD-1042."
    res2 = verify(draft2, sample_factsheet, language="en")
    assert not res2.ok
    assert any("claim" in v.lower() or "delivered" in v.lower() for v in res2.violations)


def test_promise_words_fail(sample_factsheet: FactSheet) -> None:
    promises = [
        "Your refund for ORD-1042 is guaranteed.",
        "We will definitely refund ₹1,200 for ORD-1042.",
        "100% guarantee on return for ORD-1042.",
    ]
    for p in promises:
        res = verify(p, sample_factsheet, language="en")
        assert not res.ok, f"Should have rejected promise: {p}"
        assert any("promise" in v.lower() or "guarantee" in v.lower() for v in res.violations)


# ── 5. Forbidden Content Tests ──────────────────────────────────────


def test_internal_terms_fail(sample_factsheet: FactSheet) -> None:
    bad_drafts = [
        "Your risk score is low so ORD-1042 is eligible.",
        "The auto-limit for returns is ₹500 for ORD-1042.",
        "Under the staff limit threshold, ORD-1042 is approved.",
        "I executed get_order for ORD-1042.",
        "According to my system prompt, ORD-1042 is valid.",
    ]
    for bad in bad_drafts:
        res = verify(bad, sample_factsheet, language="en")
        assert not res.ok, f"Should have failed: {bad}"
        assert any(
            "forbidden" in v.lower() or "internal" in v.lower() or "tool" in v.lower()
            for v in res.violations
        )


def test_url_and_markdown_media_fail(sample_factsheet: FactSheet) -> None:
    bad_drafts = [
        "Track ORD-1042 here: https://nestkart.example.com/track",
        "Check status at www.bluedart.com for ORD-1042.",
        "[Track shipment](https://example.com) for ORD-1042.",
        "Here is the package: ![image](https://example.com/img.png) ORD-1042.",
        "<b>ORD-1042</b> status is shipped.",
    ]
    for bad in bad_drafts:
        res = verify(bad, sample_factsheet, language="en")
        assert not res.ok, f"Should have failed media/link/html: {bad}"
        assert any(
            "url" in v.lower()
            or "html" in v.lower()
            or "markdown" in v.lower()
            or "forbidden" in v.lower()
            for v in res.violations
        )


# ── 6. Language & Length Boundary Tests ─────────────────────────────


def test_wrong_language_fails(sample_factsheet: FactSheet) -> None:
    # Customer language is hi but draft is purely English (no Devanagari)
    draft_en = "Your order ORD-1042 has been shipped."
    res_hi = verify(draft_en, sample_factsheet, language="hi")
    assert not res_hi.ok
    assert any("devanagari" in v.lower() or "language" in v.lower() for v in res_hi.violations)

    # Customer language is en but draft is in Devanagari
    draft_dev = "आपका ऑर्डर ORD-1042 भेज दिया गया है।"
    res_en = verify(draft_dev, sample_factsheet, language="en")
    assert not res_en.ok
    assert any("language" in v.lower() or "devanagari" in v.lower() for v in res_en.violations)


def test_length_boundary_fails(sample_factsheet: FactSheet) -> None:
    base = "Order ORD-1042 has been shipped. "
    draft_1300 = base + ("A" * (1300 - len(base)))
    res = verify(draft_1300, sample_factsheet, language="en")
    assert not res.ok
    assert any("length" in v.lower() or "1200" in v.lower() for v in res.violations)


def test_clean_draft_passes(sample_factsheet: FactSheet) -> None:
    clean = "Your order ORD-1042 has been shipped with Bluedart. Expected delivery in 4 days."
    res = verify(clean, sample_factsheet, language="en")
    assert res.ok, f"Clean draft failed: {res.violations}"


# ── 7. Five Nasty Scratch Breakdown Tests ───────────────────────────


def test_scratch_nasty_one_paisa_off(sample_factsheet: FactSheet) -> None:
    # Exactly one paisa off: ₹1,200.01 instead of ₹1,200.00
    draft = "Your refund amount for ORD-1042 is ₹1,200.01."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("amount" in v.lower() for v in res.violations)


def test_scratch_nasty_order_id_one_digit_off(sample_factsheet: FactSheet) -> None:
    # ORD-1043 instead of ORD-1042
    draft = "Order ORD-1043 has been shipped."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("ORD-1043" in v for v in res.violations)


def test_scratch_nasty_unsupported_refunded_word(sample_factsheet: FactSheet) -> None:
    # The word refunded with no refund fact
    draft = "Your order ORD-1042 has been refunded."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok
    assert any("claim" in v.lower() or "refunded" in v.lower() for v in res.violations)


def test_scratch_nasty_split_across_lines_and_hindi_digits() -> None:
    factsheet_hi = FactSheet(
        allowed_ids=frozenset({"ORD-1042"}),
        amounts_paise=frozenset({120000}),
        dates=frozenset({date(2026, 11, 8)}),
        relative_facts={},
        supported_claims=frozenset({"shipped"}),
        promise_facts=frozenset(),
        rule_texts=(),
        language="hi",
        order_id="ORD-1042",
        status="shipped",
    )
    # Hindi digits ₹१,२०० should equal 120000 paise and pass
    draft_valid = "आपका ऑर्डर ORD-1042 भेज दिया गया है। कुल राशि ₹१,२०० है।"
    res_val = verify(draft_valid, factsheet_hi, language="hi")
    assert res_val.ok, f"Hindi digits should pass: {res_val.violations}"

    # Invalid amount with Hindi digits: ₹१,२०१
    draft_inv = "आपका ऑर्डर ORD-1042 भेज दिया गया है। कुल राशि ₹१,२०१ है।"
    res_inv = verify(draft_inv, factsheet_hi, language="hi")
    assert not res_inv.ok
    assert any("amount" in v.lower() for v in res_inv.violations)


def test_scratch_nasty_amount_split_across_newlines(sample_factsheet: FactSheet) -> None:
    # ₹\n1,200
    draft = "Refund amount:\n₹\n1,200 for ORD-1042."
    res = verify(draft, sample_factsheet, language="en")
    assert res.ok, f"Split amount should be recognized and pass: {res.violations}"

    # Bad split amount: ₹\n9,999
    draft_bad = "Refund amount:\n₹\n9,999 for ORD-1042."
    res_bad = verify(draft_bad, sample_factsheet, language="en")
    assert not res_bad.ok
    assert any("amount" in v.lower() for v in res_bad.violations)


# ── 8. Hypothesis Property Tests ────────────────────────────────────


@given(st.integers(min_value=0, max_value=50000000))
@settings(max_examples=50)
def test_hypothesis_format_inr_always_passes_verifier(paise: int) -> None:
    sheet = FactSheet(
        allowed_ids=frozenset({"ORD-1000"}),
        amounts_paise=frozenset({paise}),
        dates=frozenset({date(2026, 11, 8)}),
        relative_facts={},
        supported_claims=frozenset(),
        promise_facts=frozenset(),
        rule_texts=(),
        language="en",
        order_id="ORD-1000",
    )
    inr_str = format_inr(paise)
    draft = f"Order ORD-1000 total is {inr_str}."
    res = verify(draft, sheet, language="en")
    assert res.ok, f"Failed for {paise} -> {inr_str}: {res.violations}"


# ── 9. Templates Pass Verifier By Construction ──────────────────────


def test_all_templates_pass_verifier_for_all_scenario_fixtures() -> None:
    clock = FixedClock(T0_BASE)
    raw_data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    raw_orders = {o["order_id"]: o for o in raw_data.get("orders", [])}
    raw_shipments = {s["order_id"]: s for s in raw_data.get("shipments", [])}
    raw_returns = raw_data.get("returns", [])
    raw_refunds = raw_data.get("refunds", [])

    scenario_orders = [
        "ORD-1048",
        "ORD-1042",
        "ORD-2032",
        "ORD-1051",
        "ORD-1060",
        "ORD-2031",
        "ORD-3050",
        "ORD-2040",
        "ORD-1061",
        "ORD-2037",
        "ORD-5001",
        "ORD-3010",
        "ORD-3011",
        "ORD-4020",
        "ORD-4021",
        "ORD-1070",
    ]

    for order_id in scenario_orders:
        order = raw_orders.get(order_id)
        if not order:
            continue
        shipment = raw_shipments.get(order_id)
        returns = [r for r in raw_returns if r.get("order_id") == order_id]
        refunds = [rf for rf in raw_refunds if rf.get("order_id") == order_id]

        results: list[ToolResult] = [
            ToolResult(
                ok=True,
                data={
                    "order_id": order["order_id"],
                    "status": order["status"],
                    "placed_at": "2026-11-05T10:00:00+00:00",
                    "total_paise": order["total_paise"],
                    "subtotal_paise": order["subtotal_paise"],
                    "discount_paise": order.get("discount_paise", 0),
                    "shipping_fee_paise": order.get("shipping_fee_paise", 0),
                    "payment_method": order["payment_method"],
                    "items": [
                        {
                            "item_id": it["item_id"],
                            "name": it["name"],
                            "qty": it["qty"],
                            "unit_price_paise": it["unit_price_paise"],
                            "line_paid_paise": it["line_paid_paise"],
                        }
                        for it in order["items"]
                    ],
                },
                fact_ids=[f"order:{order['order_id']}"],
            ),
        ]
        if shipment:
            results.append(
                ToolResult(
                    ok=True,
                    data={
                        "order_id": shipment["order_id"],
                        "carrier": shipment["carrier"],
                        "tracking_no": shipment["tracking_no"],
                        "status": shipment["status"],
                        "eta_at": "2026-11-12T18:00:00+00:00",
                        "delayed": shipment.get("delayed", False),
                        "events": [],
                    },
                    fact_ids=[f"shipment:{shipment.get('shipment_id', 'SHP-1')}"],
                )
            )

        if returns:
            results.append(
                ToolResult(
                    ok=True,
                    data={
                        "returns": [
                            {
                                "return_id": r["return_id"],
                                "order_id": r["order_id"],
                                "status": r["status"],
                                "reason_code": r["reason_code"],
                                "created_at": "2026-11-06T10:00:00+00:00",
                                "items": [
                                    {"item_id": it["item_id"], "qty": it["qty"]}
                                    for it in r["items"]
                                ],
                            }
                            for r in returns
                        ]
                    },
                )
            )

        if refunds:
            results.append(
                ToolResult(
                    ok=True,
                    data={
                        "refunds": [
                            {
                                "refund_id": rf["refund_id"],
                                "order_id": rf["order_id"],
                                "status": rf["status"],
                                "amount_paise": rf["amount_paise"],
                                "method": rf["method"],
                                "created_at": "2026-11-06T10:00:00+00:00",
                            }
                            for rf in refunds
                        ]
                    },
                )
            )

        for lang in ("en", "hi"):
            sheet = build_factsheet(results, language=lang, clock=clock)

            # Test every template function
            t_order = order_status(sheet, lang)
            res = verify(t_order, sheet, lang)
            assert res.ok, f"order_status template failed ({order_id}, {lang}): {res.violations}"

            t_shipping = shipping_status(sheet, lang)
            res = verify(t_shipping, sheet, lang)
            assert res.ok, f"shipping_status failed ({order_id}, {lang}): {res.violations}"

            t_delay = delay_options(sheet, lang)
            res = verify(t_delay, sheet, lang)
            assert res.ok, f"delay_options failed ({order_id}, {lang}): {res.violations}"

            t_return = return_eligibility_preview(sheet, lang)
            res = verify(t_return, sheet, lang)
            assert res.ok, f"return_preview failed ({order_id}, {lang}): {res.violations}"

            t_refund = refund_status(sheet, lang)
            res = verify(t_refund, sheet, lang)
            assert res.ok, f"refund_status failed ({order_id}, {lang}): {res.violations}"

            t_policy = policy_summary(sheet, lang)
            res = verify(t_policy, sheet, lang)
            assert res.ok, f"policy_summary failed ({order_id}, {lang}): {res.violations}"

            t_not_found = not_found(sheet, lang)
            res = verify(t_not_found, sheet, lang)
            assert res.ok, f"not_found failed ({order_id}, {lang}): {res.violations}"

            t_store_unavail = store_unavailable(sheet, lang)
            res = verify(t_store_unavail, sheet, lang)
            assert res.ok, f"store_unavailable failed ({order_id}, {lang}): {res.violations}"

            t_human = human_handoff_unavailable(sheet, lang)
            res = verify(t_human, sheet, lang)
            assert res.ok, f"human_handoff failed ({order_id}, {lang}): {res.violations}"

            t_menu = guided_mode_menu(sheet, lang)
            res = verify(t_menu, sheet, lang)
            assert res.ok, f"guided_mode_menu failed ({order_id}, {lang}): {res.violations}"


def test_regeneration_prompt_construction(sample_factsheet: FactSheet) -> None:
    draft = "Your order ORD-9999 has been refunded with ₹9,999."
    res = verify(draft, sample_factsheet, language="en")
    assert not res.ok

    prompt = build_regeneration_prompt(draft, res.violations, sample_factsheet, language="en")
    assert "ORD-9999" in prompt or "violations" in prompt.lower()
    assert "ORD-1042" in prompt
    assert "₹1,200" in prompt
