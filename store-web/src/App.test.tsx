import { describe, expect, it } from "vitest";
import { apiClient } from "./api/client";
import {
  fetchSellerApprovals,
  fetchSellerAudit,
  fetchSellerCases,
  fetchSellerOverview,
} from "./api/sellerShield";
import { LoginPage } from "./App";
import { formatConfirmProposalMessage } from "./components/ChatWidget";

describe("Web skeleton test suite", () => {
  it("initializes apiClient with GET method", () => {
    expect(typeof apiClient.GET).toBe("function");
  });

  it("exports LoginPage component", () => {
    expect(typeof LoginPage).toBe("function");
  });

  it("exports SellerShield API helpers", () => {
    expect(typeof fetchSellerOverview).toBe("function");
    expect(typeof fetchSellerApprovals).toBe("function");
    expect(typeof fetchSellerCases).toBe("function");
    expect(typeof fetchSellerAudit).toBe("function");
  });

  it("formats proposal confirm replies truthfully without RET-SUCCESS fallback", () => {
    const cancelledMsg = formatConfirmProposalMessage("cancel", "ORD-4021", {
      status: "succeeded",
      execution_id: "EXE-123",
    });
    expect(cancelledMsg).toContain("ORD-4021");
    expect(cancelledMsg).toContain("cancelled");
    expect(cancelledMsg).not.toContain("RET-SUCCESS");

    const pendingMsg = formatConfirmProposalMessage("cancel", "ORD-4021", {
      status: "pending_approval",
      approval_id: "APR-99",
    });
    expect(pendingMsg).toContain("APR-99");
    expect(pendingMsg).toContain("seller review");
    expect(pendingMsg).not.toContain("RET-SUCCESS");

    const retMsg = formatConfirmProposalMessage("return", "ORD-2031", {
      status: "succeeded",
      return_id: "RET-2031",
    });
    expect(retMsg).toContain("RET-2031");
  });
});

