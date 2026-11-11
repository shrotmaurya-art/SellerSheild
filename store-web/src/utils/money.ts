/**
 * Format integer paise into Indian Rupee currency format.
 * Golden rule: 1 Rupee = 100 paise. Never uses float math for money calculations.
 *
 * @param paise - Integer amount in paise (e.g. 89900)
 * @returns Formatted string (e.g. "₹899.00")
 */
export function formatMoney(paise: number | null | undefined): string {
  if (paise == null || isNaN(paise)) {
    return "₹0.00";
  }

  const sign = paise < 0 ? "-" : "";
  const absPaise = Math.abs(Math.round(paise));
  const rupees = Math.floor(absPaise / 100);
  const remainingPaise = absPaise % 100;
  const paiseStr = remainingPaise.toString().padStart(2, "0");

  // Format integer rupees with Indian number grouping (last 3, then groups of 2)
  const rupeesStr = rupees.toLocaleString("en-IN");

  return `${sign}₹${rupeesStr}.${paiseStr}`;
}

