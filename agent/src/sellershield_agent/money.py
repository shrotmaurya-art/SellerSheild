"""Integer-paise money helpers (KP-05: never use float for money).

All monetary values are ``int`` paise (1 rupee = 100 paise).
Formatting and parsing use **integer arithmetic only** — no float
at any point.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# Type alias — plain int so arithmetic works unchanged.
Paise = int


# ── formatting ────────────────────────────────────────────────────


def format_inr(paise: int) -> str:
    """Format *paise* as a human-readable INR string.

    Whole-rupee amounts omit decimals: ``format_inr(120000)`` → ``"₹1,200"``.
    Fractional amounts show two decimals: ``format_inr(59899)`` → ``"₹598.99"``.

    Raises :class:`TypeError` if *paise* is not ``int``.
    Raises :class:`ValueError` if *paise* is negative.
    """
    if not isinstance(paise, int):
        raise TypeError(f"paise must be int, got {type(paise).__name__}")
    if paise < 0:
        raise ValueError(f"paise must be >= 0, got {paise}")

    rupees = paise // 100
    remainder = paise % 100

    if remainder == 0:
        return f"₹{rupees:,}"
    else:
        # Zero-pad the paise part to 2 digits (10 → "10", 1 → "01")
        return f"₹{rupees:,}.{remainder:02d}"


# ── parsing ───────────────────────────────────────────────────────


def parse_rupees(text: str) -> int:
    """Parse a rupee string into integer paise.

    Accepts whole numbers (``"1200"`` → 120000) and decimals with up
    to two places (``"598.99"`` → 59899).  Uses :class:`~decimal.Decimal`
    internally — never float.

    Raises :class:`TypeError` if *text* is a ``float`` (must be ``str``).
    Raises :class:`ValueError` on negative values or more than 2 decimal places.
    """
    if isinstance(text, float):
        raise TypeError("pass a str, not float — float causes paise drift")
    if not isinstance(text, str):
        raise TypeError(f"expected str, got {type(text).__name__}")

    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse {text!r} as a rupee amount") from exc

    if d < 0:
        raise ValueError(f"amount must be >= 0, got {d}")

    # Reject more than 2 decimal places to prevent silent truncation
    sign, digits, exponent = d.as_tuple()
    # exponent is negative for decimals: Decimal("100.999").as_tuple().exponent == -3
    if isinstance(exponent, int) and exponent < -2:
<<<<<<< HEAD
        raise ValueError(f"at most 2 decimal places allowed, got {text!r}")
=======
        raise ValueError(
            f"at most 2 decimal places allowed, got {text!r}"
        )
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be

    # Convert to paise: multiply by 100 using Decimal (no float)
    paise_decimal = d * 100
    paise = int(paise_decimal)

    # Sanity: the conversion must be exact (no fractional sub-paise)
    if paise_decimal != paise:
        raise ValueError(f"sub-paise precision in {text!r}")

    return paise


# ── allocation ────────────────────────────────────────────────────


def allocate_largest_remainder(total_paise: int, weights: list[int]) -> list[int]:
    """Distribute *total_paise* across buckets proportional to *weights*.

    Uses the Hamilton / largest-remainder method with **pure integer
    arithmetic**.  The result always sums exactly to *total_paise*.

    Ties (equal fractional remainders) are broken by index — the
    earlier bucket wins.

    Raises :class:`TypeError` if *total_paise* or any weight is not ``int``.
    Raises :class:`ValueError` if *total_paise* < 0, weights is empty,
    any weight < 0, or all weights are zero.
    """
    if not isinstance(total_paise, int):
        raise TypeError(f"total_paise must be int, got {type(total_paise).__name__}")
    if total_paise < 0:
        raise ValueError(f"total_paise must be >= 0, got {total_paise}")
    if not weights:
        raise ValueError("weights must not be empty")

    for i, w in enumerate(weights):
        if not isinstance(w, int):
            raise TypeError(f"weight[{i}] must be int, got {type(w).__name__}")
        if w < 0:
            raise ValueError(f"weight[{i}] must be >= 0, got {w}")

    total_weight = sum(weights)
    if total_weight == 0:
        raise ValueError("weights must not all be zero")

    # Floor shares and remainders — pure integer math
    # exact_numerator[i] = total_paise * weight[i]
    # floor[i] = exact_numerator[i] // total_weight
    # remainder_numerator[i] = exact_numerator[i] % total_weight
    floors: list[int] = []
    remainders: list[tuple[int, int]] = []  # (remainder_numerator, index)

    for i, w in enumerate(weights):
        numerator = total_paise * w
        floor = numerator // total_weight
        remainder = numerator % total_weight
        floors.append(floor)
        remainders.append((remainder, i))

    shortfall = total_paise - sum(floors)

    # Distribute the shortfall one paise at a time to the largest remainders.
    # Sort descending by remainder, then ascending by index (stable sort for ties).
    remainders.sort(key=lambda r: (-r[0], r[1]))

    for k in range(shortfall):
        idx = remainders[k][1]
        floors[idx] += 1

    return floors
<<<<<<< HEAD
=======

>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
