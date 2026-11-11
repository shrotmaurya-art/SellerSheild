import { describe, expect, it, vi, beforeEach } from "vitest";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { ConfirmationCard } from "./ConfirmationCard";

describe("ConfirmationCard Component Tests", () => {
  beforeEach(() => {
    (globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  });

  // Helper to mount component in a fresh DOM container
  const renderCard = (props: React.ComponentProps<typeof ConfirmationCard>) => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    act(() => {
      root.render(<ConfirmationCard {...props} />);
    });

    return {
      container,
      cleanup: () => {
        act(() => {
          root.unmount();
        });
        container.remove();
      },
    };
  };

  describe("1. Expiry behavior (10-minute limit)", () => {
    it("renders countdown correctly for active confirmation card", () => {
      // 10 minutes in the future
      const futureTime = Date.now() + 10 * 60 * 1000;
      const { container, cleanup } = renderCard({
        title: "Confirm Return",
        expiresAt: futureTime,
      });

      const badge = container.querySelector('[data-testid="countdown-badge"]');
      expect(badge).toBeTruthy();
      expect(badge?.textContent).toMatch(/10:00|09:59/);

      const confirmBtn = container.querySelector(
        '[data-testid="confirm-button"]'
      ) as HTMLButtonElement;
      const cancelBtn = container.querySelector(
        '[data-testid="cancel-button"]'
      ) as HTMLButtonElement;

      expect(confirmBtn.disabled).toBe(false);
      expect(cancelBtn.disabled).toBe(false);

      cleanup();
    });

    it("disables buttons and shows expired message when time has elapsed", () => {
      // Past expiration timestamp
      const expiredTime = Date.now() - 5000;
      const onConfirm = vi.fn();
      const onCancel = vi.fn();

      const { container, cleanup } = renderCard({
        title: "Confirm Return",
        expiresAt: expiredTime,
        onConfirm,
        onCancel,
      });

      const badge = container.querySelector('[data-testid="countdown-badge"]');
      expect(badge?.textContent).toContain("00:00 (Expired)");

      const expiredMsg = container.querySelector('[data-testid="expired-message"]');
      expect(expiredMsg).toBeTruthy();
      expect(expiredMsg?.textContent).toContain("expired (10 min limit)");

      const confirmBtn = container.querySelector(
        '[data-testid="confirm-button"]'
      ) as HTMLButtonElement;
      const cancelBtn = container.querySelector(
        '[data-testid="cancel-button"]'
      ) as HTMLButtonElement;

      expect(confirmBtn.disabled).toBe(true);
      expect(cancelBtn.disabled).toBe(true);

      // Attempting to click disabled buttons should NOT trigger handlers
      act(() => {
        confirmBtn.click();
        cancelBtn.click();
      });

      expect(onConfirm).not.toHaveBeenCalled();
      expect(onCancel).not.toHaveBeenCalled();

      cleanup();
    });
  });

  describe("2. Double-click protection", () => {
    it("prevents double-submission and calls onConfirm only once on rapid clicks", async () => {
      let resolvePromise: () => void = () => {};
      const pendingPromise = new Promise<void>((resolve) => {
        resolvePromise = resolve;
      });

      const onConfirm = vi.fn().mockImplementation(() => pendingPromise);

      const { container, cleanup } = renderCard({
        title: "Confirm Refund",
        onConfirm,
      });

      const confirmBtn = container.querySelector(
        '[data-testid="confirm-button"]'
      ) as HTMLButtonElement;
      expect(confirmBtn.disabled).toBe(false);

      // Rapidly trigger two click events (simulating double-click)
      act(() => {
        confirmBtn.click();
        confirmBtn.click();
      });

      // Assert handler was only invoked once
      expect(onConfirm).toHaveBeenCalledTimes(1);

      // Button should now be disabled and display "Processing..."
      expect(confirmBtn.disabled).toBe(true);
      expect(confirmBtn.textContent).toContain("Processing...");

      // Resolve the pending action
      await act(async () => {
        resolvePromise();
        await pendingPromise;
      });

      // Successful confirmation status should appear
      const confirmedMsg = container.querySelector('[data-testid="confirmed-message"]');
      expect(confirmedMsg).toBeTruthy();
      expect(confirmBtn.disabled).toBe(true);

      cleanup();
    });
  });
});
