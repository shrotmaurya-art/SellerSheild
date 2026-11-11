import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiClient,
  getAccessToken,
  refreshAccessToken,
  setAccessToken,
} from "./client";

describe("API client & In-Memory Token Management", () => {
  beforeEach(() => {
    setAccessToken(null);
    vi.restoreAllMocks();
  });

  it("stores token strictly in memory and never touches localStorage or sessionStorage", () => {
    setAccessToken("test_token_123");
    expect(getAccessToken()).toBe("test_token_123");

    // Golden rule: verify storage is untouched
    const localVal =
      typeof window !== "undefined" && window.localStorage
        ? window.localStorage.getItem("access_token")
        : null;
    const sessionVal =
      typeof window !== "undefined" && window.sessionStorage
        ? window.sessionStorage.getItem("access_token")
        : null;

    expect(localVal).toBeNull();
    expect(sessionVal).toBeNull();

    setAccessToken(null);
    expect(getAccessToken()).toBeNull();
  });

  it("performs refreshAccessToken with credentials and CSRF header", async () => {
    // Set a mock cookie for nestkart_csrf
    Object.defineProperty(document, "cookie", {
      writable: true,
      value: "nestkart_csrf=mock_csrf_val_123",
    });

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ access_token: "refreshed_access_token" }),
    });
    globalThis.fetch = mockFetch;

    const token = await refreshAccessToken();
    expect(token).toBe("refreshed_access_token");
    expect(getAccessToken()).toBe("refreshed_access_token");

    expect(mockFetch).toHaveBeenCalledWith(
      "/api/v1/auth/refresh",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        headers: expect.objectContaining({
          "X-CSRF-Token": "mock_csrf_val_123",
        }),
      }),
    );
  });

  it("refreshes token ONCE when encountering a 401 response and retries request", async () => {
    setAccessToken("expired_token");

    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation((input: RequestInfo | URL, _init?: RequestInit) => {
      callCount++;
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";

      // Call 1: Original request returns 401
      if (url.includes("/api/v1/orders") && callCount === 1) {
        return Promise.resolve(
          new Response(JSON.stringify({ error: "Unauthorized" }), {
            status: 401,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      // Call 2: Refresh token request returns 200 with new token
      if (url.includes("/api/v1/auth/refresh")) {
        return Promise.resolve(
          new Response(JSON.stringify({ access_token: "new_refreshed_token" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      // Call 3: Retried request with new Authorization header succeeds with 200
      if (url.includes("/api/v1/orders") && callCount >= 3) {
        return Promise.resolve(
          new Response(JSON.stringify({ items: [], has_more: false }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      return Promise.resolve(new Response(null, { status: 404 }));
    });

    globalThis.fetch = mockFetch;

    const res = await apiClient.GET("/api/v1/orders");
    expect(res.response.status).toBe(200);
    expect(getAccessToken()).toBe("new_refreshed_token");
  });

  it("does not enter infinite retry loop if retried request still returns 401", async () => {
    setAccessToken("expired_token");

    let refreshCalls = 0;
    const mockFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof Request ? input.url : "";

      if (url.includes("/api/v1/auth/refresh")) {
        refreshCalls++;
        return Promise.resolve(
          new Response(JSON.stringify({ access_token: "second_token" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      // Always returns 401
      return Promise.resolve(
        new Response(JSON.stringify({ error: "Unauthorized" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });

    globalThis.fetch = mockFetch;

    const res = await apiClient.GET("/api/v1/orders");
    expect(res.response.status).toBe(401);
    // Refresh should only be called once, not looped
    expect(refreshCalls).toBe(1);
  });
});