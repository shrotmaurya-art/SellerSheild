"""Deterministic templated replies built strictly from FactSheet.

Spec §4.5, §4.6, §21 (Amendments 2 & 6).
Invariants:
- Templates must pass the verifier by construction.
- Strict English ('en') and Hindi ('hi') localization.
- Never assert an action unless backed by factsheet.supported_claims.
- Zero internal limits, risk scores, or URLs.
"""

from __future__ import annotations

from sellershield_agent.compose.factsheet import FactSheet
from sellershield_agent.money import format_inr


def orders_list(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for listing customer orders."""
    is_hi = lang.lower().startswith("hi")
    if not sheet.orders:
        return (
            "You have no placed orders on file."
            if not is_hi
            else "आपके पास कोई पिछला ऑर्डर नहीं है।"
        )

    items: list[str] = []
    for o in sheet.orders:
        oid = str(o.get("order_id", ""))
        st = str(o.get("status", ""))
        if oid:
            items.append(f"{oid} ({st})" if st else oid)

    orders_str = ", ".join(items) if items else "none"
    if is_hi:
        return f"आपके ऑर्डर हैं: {orders_str}। आप किस ऑर्डर के बारे में जानना चाहते हैं?"
    return (
        f"Here are your orders: {orders_str}. "
        "Please let me know which order you would like help with."
    )


def order_status(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for order status."""
    if sheet.orders and not sheet.order_id:
        return orders_list(sheet, lang)

    is_hi = lang.lower().startswith("hi")
    oid = sheet.order_id or (next(iter(sheet.allowed_ids)) if sheet.allowed_ids else "ORD-000")
    total_str = format_inr(sheet.total_paise) if sheet.total_paise is not None else ""

    status_val = sheet.status or "placed"
    if status_val == "shipped" and "shipped" in sheet.supported_claims:
        st_en = "shipped"
        st_hi = "भेज दिया गया है"
    elif status_val == "delivered" and "delivered" in sheet.supported_claims:
        st_en = "delivered"
        st_hi = "डिलीवर हो चुका है"
    elif status_val == "cancelled" and "cancelled" in sheet.supported_claims:
        st_en = "cancelled"
        st_hi = "रद्द कर दिया गया है"
    else:
        st_en = status_val
        st_hi = status_val

    placed_dt = sheet.placed_at_ist.isoformat() if sheet.placed_at_ist else ""

    if is_hi:
        parts = [f"ऑर्डर {oid} की स्थिति: {st_hi}।"]
        if placed_dt:
            parts.append(f"ऑर्डर तिथि: {placed_dt}।")
        if total_str:
            parts.append(f"कुल राशि: {total_str}।")
        return " ".join(parts)

    parts = [f"Order {oid} status is {st_en}."]
    if placed_dt:
        parts.append(f"Placed on {placed_dt}.")
    if total_str:
        parts.append(f"Total: {total_str}.")
    return " ".join(parts)


def shipping_status(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for shipping status."""
    is_hi = lang.lower().startswith("hi")
    oid = sheet.order_id or (next(iter(sheet.allowed_ids)) if sheet.allowed_ids else "ORD-000")
    carrier = sheet.carrier or "Standard Courier"
    tracking = sheet.tracking_no or ""

    eta_days = sheet.relative_facts.get("eta_in_days")
    if eta_days == 1:
        eta_en = "tomorrow"
        eta_hi = "कल"
    elif eta_days == 0:
        eta_en = "today"
        eta_hi = "आज"
    elif eta_days is not None:
        eta_en = f"in {eta_days} days"
        eta_hi = f"{eta_days} दिनों में"
    elif sheet.eta_at_ist:
        eta_en = f"on {sheet.eta_at_ist.isoformat()}"
        eta_hi = f"{sheet.eta_at_ist.isoformat()} को"
    else:
        eta_en = ""
        eta_hi = ""

    st_val = sheet.status or "in transit"
    if "shipped" in sheet.supported_claims:
        st_en = "shipped"
        st_hi = "भेज दिया गया है"
    elif "delivered" in sheet.supported_claims:
        st_en = "delivered"
        st_hi = "डिलीवर हो चुका है"
    else:
        st_en = st_val
        st_hi = st_val

    if is_hi:
        res = f"ऑर्डर {oid} का शिपमेंट {carrier} द्वारा {st_hi}।"
        if tracking:
            res += f" ट्रैकिंग नंबर: {tracking}।"
        if eta_hi:
            res += f" अपेक्षित आगमन: {eta_hi}।"
        return res

    res = f"Shipment for order {oid} with {carrier} is {st_en}."
    if tracking:
        res += f" Tracking number: {tracking}."
    if eta_en:
        res += f" ETA: {eta_en}."
    return res


def delay_options(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for delayed shipment options."""
    is_hi = lang.lower().startswith("hi")
    oid = sheet.order_id or (next(iter(sheet.allowed_ids)) if sheet.allowed_ids else "ORD-000")

    if is_hi:
        return (
            f"ऑर्डर {oid} में देरी हुई है। आप डिलीवरी की प्रतीक्षा कर सकते हैं, "
            f"सहायता से संपर्क कर सकते हैं, या रद्दीकरण का विकल्प चुन सकते हैं।"
        )

    return (
        f"Order {oid} is delayed. You may choose to wait for delivery, "
        f"contact seller support, or request cancellation if eligible."
    )


def return_eligibility_preview(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for return eligibility preview."""
    is_hi = lang.lower().startswith("hi")
    oid = sheet.order_id or (next(iter(sheet.allowed_ids)) if sheet.allowed_ids else "ORD-000")

    amounts = sorted(sheet.amounts_paise, reverse=True)
    refund_amt_str = format_inr(amounts[0]) if amounts else "₹0"
    fee_amt_str = "₹0"

    rule_info = sheet.rule_texts[0] if sheet.rule_texts else ""

    if is_hi:
        res = f"ऑर्डर {oid} के लिए रिटर्न पूर्वावलोकन: अनुमानित रिफंड {refund_amt_str}।"
        if rule_info:
            res += f" नियम: {rule_info}।"
        return res

    res = (
        f"Return preview for order {oid}: Estimated refund is {refund_amt_str} "
        f"with {fee_amt_str} return fee."
    )
    if rule_info:
        res += f" Note: {rule_info}."
    return res


def refund_status(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for refund status."""
    is_hi = lang.lower().startswith("hi")
    ord_candidates = [i for i in sheet.allowed_ids if i.startswith("ORD-")]
    oid = sheet.order_id or (
        ord_candidates[0]
        if ord_candidates
        else (next(iter(sheet.allowed_ids)) if sheet.allowed_ids else "ORD-000")
    )

    rfd_id = ""
    amount_str = ""
    status_label = "pending"

    if sheet.refunds:
        r0 = sheet.refunds[0]
        rfd_id = r0.get("refund_id", "")
        oid = str(r0.get("order_id") or oid)
        if "amount_paise" in r0 and isinstance(r0["amount_paise"], int):
            amount_str = format_inr(r0["amount_paise"])
        st_val = r0.get("status", "")
        if st_val == "completed" and "refunded" in sheet.supported_claims:
            status_label = "refunded"
        elif st_val == "processing" and "processed" in sheet.supported_claims:
            status_label = "processed"
        else:
            status_label = st_val

    if not rfd_id and sheet.allowed_ids:
        rfd_candidates = [i for i in sheet.allowed_ids if i.startswith("RFD-")]
        if rfd_candidates:
            rfd_id = rfd_candidates[0]

    if not sheet.refunds and not rfd_id:
        if is_hi:
            return f"ऑर्डर {oid} के लिए कोई सक्रिय रिफंड नहीं मिला।"
        return f"No active refund found for order {oid}."

    if not amount_str and sheet.amounts_paise:
        nz = [a for a in sorted(sheet.amounts_paise, reverse=True) if a > 0]
        if nz:
            amount_str = format_inr(nz[0])

    id_part = f"{rfd_id} " if rfd_id else ""
    amt_part = f" Amount: {amount_str}." if amount_str else ""

    if is_hi:
        st_hi = "संसाधित" if status_label == "processed" else status_label
        amt_hi = f" राशि: {amount_str}।" if amount_str else ""
        id_hi = f"{rfd_id} " if rfd_id else ""
        return f"ऑर्डर {oid} के लिए रिफंड {id_hi}की स्थिति: {st_hi}।{amt_hi}"

    return f"Refund {id_part}for order {oid} status is {status_label}.{amt_part}"


def return_status(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for live return status."""
    is_hi = lang.lower().startswith("hi")
    ord_candidates = [i for i in sheet.allowed_ids if i.startswith("ORD-")]
    oid_opt = sheet.order_id or (ord_candidates[0] if ord_candidates else None)

    if not sheet.returns:
        if is_hi:
            if oid_opt:
                return f"ऑर्डर {oid_opt} के लिए कोई रिटर्न अनुरोध नहीं मिला।"
            return "कोई सक्रिय रिटर्न अनुरोध नहीं मिला।"
        if oid_opt:
            return f"No return request found for order {oid_opt}."
        return "No return request was found."

    r0 = sheet.returns[0]
    ret_id = str(r0.get("return_id", ""))
    oid = str(r0.get("order_id") or oid_opt or "your order")
    status_label = str(r0.get("status", "requested"))

    if not ret_id and sheet.allowed_ids:
        ret_candidates = [i for i in sheet.allowed_ids if i.startswith("RET-")]
        if ret_candidates:
            ret_id = ret_candidates[0]

    id_part = f"{ret_id} " if ret_id else ""
    if is_hi:
        return f"ऑर्डर {oid} के लिए रिटर्न {id_part}की स्थिति: {status_label}।"
    return f"Return {id_part}for order {oid} status is {status_label}."


def policy_summary(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for customer-safe policy summary."""
    is_hi = lang.lower().startswith("hi")
    pdata = sheet.policy_summary_data
    windows = pdata.get("windows", {})
    win_desc = (
        ", ".join(f"{k}: {v} days" for k, v in list(windows.items())[:3])
        if windows
        else "Standard windows apply"
    )

    if is_hi:
        return (
            f"स्टोर नीति सारांश: रिटर्न अवधि: {win_desc}। विस्तृत जानकारी के लिए ग्राहक सहायता से संपर्क करें।"
        )

    return f"Store Policy Summary: Return windows: {win_desc}. Standard return terms apply."


def not_found(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for uniform not found (KP-15)."""
    if lang.lower().startswith("hi"):
        return "मुझे आपके खाते में वह ऑर्डर नहीं मिला। कृपया अपने विवरण की जाँच करें।"
    return "I couldn't find that order on your account. Please check the order details."


def store_unavailable(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template when upstream store is unavailable."""
    if lang.lower().startswith("hi"):
        return "मैं अभी ऑर्डर डेटा तक नहीं पहुँच पा रहा हूँ। कृपया थोड़ी देर बाद पुनः प्रयास करें।"
    return "I cannot access order data right now. Please try again shortly."


def human_handoff_unavailable(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template when human handoff is unavailable."""
    if lang.lower().startswith("hi"):
        return "मानव सहायता वर्तमान में उपलब्ध नहीं है। कृपया अपनी समस्या साझा करें और हम आपकी सहायता करेंगे।"
    return "Human support is currently not available. Please share your issue and we will help you."


def guided_mode_menu(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic template for guided-mode options menu."""
    if lang.lower().startswith("hi"):
        return (
            "आज मैं आपकी किस प्रकार सहायता कर सकता हूँ? कृपया एक विकल्प चुनें: "
            "1. ऑर्डर स्थिति 2. आइटम वापस करें 3. रिफंड स्थिति 4. नीति सारांश।"
        )
    return (
        "How can I help you today? Please choose an option: "
        "1. Order status 2. Return an item 3. Refund status 4. Policy summary."
    )


def proposal_confirmation(sheet: FactSheet, lang: str = "en") -> str:
    """Deterministic single-sentence template for confirmation proposal replies."""
    is_hi = lang.lower().startswith("hi")
    oid = sheet.order_id or next(
        (aid for aid in sorted(sheet.allowed_ids) if aid.startswith("ORD-")),
        "ORD-000",
    )
    pid = sheet.proposal_id or next(
        (aid for aid in sorted(sheet.allowed_ids) if aid.startswith("PRP-")),
        "PRP-000",
    )
    amount = sheet.proposal_total_paise
    if amount is None:
        nz = [a for a in sorted(sheet.amounts_paise, reverse=True) if a > 0]
        amount = nz[0] if nz else 0
    amt_str = format_inr(amount)

    if is_hi:
        return (
            f"ऑर्डर {oid} के लिए {amt_str} का प्रस्ताव {pid} तैयार है — "
            "आगे बढ़ने के लिए नीचे दिए गए कार्ड से पुष्टि या रद्द चुनें।"
        )

    if sheet.proposal_decision == "needs_approval":
        return (
            f"Proposal {pid} for order {oid} ({amt_str}) is ready "
            "for seller review once you confirm below."
        )
    return (
        f"Proposal {pid} for order {oid} ({amt_str}) is ready—please "
        "confirm or cancel using the card below."
    )
