import { describe, expect, it } from "vitest";
import { formatMoney } from "./money";

describe("formatMoney utility", () => {
  it("formats 89900 paise to ₹899.00", () => {
    expect(formatMoney(89900)).toBe("₹899.00");
  });

  it("formats 120000 paise to ₹1,200.00", () => {
    expect(formatMoney(120000)).toBe("₹1,200.00");
  });

  it("formats small paise values like 499 to ₹4.99", () => {
    expect(formatMoney(499)).toBe("₹4.99");
  });

  it("formats single digit paise like 5 to ₹0.05", () => {
    expect(formatMoney(5)).toBe("₹0.05");
  });

  it("formats zero paise to ₹0.00", () => {
    expect(formatMoney(0)).toBe("₹0.00");
  });

  it("handles null or undefined gracefully", () => {
    expect(formatMoney(null)).toBe("₹0.00");
    expect(formatMoney(undefined)).toBe("₹0.00");
  });

  it("formats negative paise amounts with minus sign", () => {
    expect(formatMoney(-5000)).toBe("-₹50.00");
  });
});

