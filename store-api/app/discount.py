import math


def allocate_discount_largest_remainder(
    item_amounts_paise: list[int],
    total_discount_paise: int,
) -> list[int]:
    """
    Allocates total_discount_paise across items proportionally to their item_amounts_paise
    using the Largest Remainder (Hare-Niemeyer) method in integer paise.

    Guarantees:
    - Each allocated amount is an integer.
    - sum(allocated_amounts) == min(total_discount_paise, sum(item_amounts_paise)) exactly.
    - No float rounding errors or missing paise.
    """
    n = len(item_amounts_paise)
    if n == 0:
        return []

    if total_discount_paise <= 0:
        return [0] * n

    total_amount = sum(item_amounts_paise)
    if total_amount <= 0:
        return [0] * n

    # Cap discount if it exceeds total amount
    effective_discount = min(total_discount_paise, total_amount)

    # Compute exact shares, integer parts, and remainders
    integer_parts = []
    remainders = []
    for i, amount in enumerate(item_amounts_paise):
        exact_share = (amount * effective_discount) / total_amount
        int_part = math.floor(exact_share)
        rem = exact_share - int_part
        integer_parts.append(int_part)
        remainders.append((rem, i))

    shortfall = effective_discount - sum(integer_parts)

    # Sort indices by remainder descending, tie-breaking by original index
    remainders.sort(key=lambda x: (-x[0], x[1]))

    allocated = list(integer_parts)
    for k in range(shortfall):
        idx = remainders[k][1]
        allocated[idx] += 1

    return allocated
