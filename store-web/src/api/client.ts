import createClient from "openapi-fetch";
import type { paths } from "./schema";

// In-memory access token storage ONLY (Never localStorage or sessionStorage)
let inMemoryAccessToken: string | null = null;
let refreshPromise: Promise<string | null> | null = null;

export function getAccessToken(): string | null {
  return inMemoryAccessToken;
}

export function setAccessToken(token: string | null): void {
  inMemoryAccessToken = token;
}

/**
 * Helper to read non-httpOnly cookies from document.cookie (e.g. nestkart_csrf).
 */
export function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(
    new RegExp("(^|;\\s*)" + name + "=([^;]*)"),
  );
  return match ? decodeURIComponent(match[2]) : null;
}

/**
 * Perform token refresh using the rotating httpOnly cookie and double-submit CSRF.
 * Deduplicates concurrent refresh calls.
 */
export async function refreshAccessToken(): Promise<string | null> {
  if (refreshPromise) {
    return refreshPromise;
  }

  refreshPromise = (async () => {
    try {
      const csrfToken = getCookie("nestkart_csrf") || "";
      const requestId =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `req-${Date.now()}`;

      const res = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "X-Request-ID": requestId,
        },
      });

      if (!res.ok) {
        setAccessToken(null);
        return null;
      }

      const data = await res.json();
      if (data && typeof data.access_token === "string") {
        setAccessToken(data.access_token);
        return data.access_token;
      }

      setAccessToken(null);
      return null;
    } catch {
      setAccessToken(null);
      return null;
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

const getBaseUrl = (): string => {
  if (typeof window !== "undefined" && window.location?.origin && window.location.origin !== "null") {
    return window.location.origin;
  }
  return "http://localhost:5173";
};

export const apiClient = createClient<paths>({
  baseUrl: getBaseUrl(),
  fetch: (input: Request) => globalThis.fetch(input),
});

// Middleware: adds Authorization, X-Request-ID, and handles 401 refresh-once
apiClient.use({
  async onRequest({ request, schemaPath }) {
    // Attach in-memory access token if present
    const token = getAccessToken();
    if (token && !request.headers.has("Authorization")) {
      request.headers.set("Authorization", `Bearer ${token}`);
    }

    // Attach X-Request-ID if not present
    if (!request.headers.has("X-Request-ID")) {
      const requestId =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : `req-${Date.now()}`;
      request.headers.set("X-Request-ID", requestId);
    }

    // Pass CSRF token for mutating auth endpoints if available
    if (schemaPath.startsWith("/api/v1/auth")) {
      const csrf = getCookie("nestkart_csrf");
      if (csrf && !request.headers.has("X-CSRF-Token")) {
        request.headers.set("X-CSRF-Token", csrf);
      }
    }

    return request;
  },

  async onResponse({ response, request, schemaPath }) {
    // Don't intercept auth login/refresh/logout failures
    if (
      schemaPath === "/api/v1/auth/login" ||
      schemaPath === "/api/v1/auth/refresh" ||
      schemaPath === "/api/v1/auth/logout"
    ) {
      return response;
    }

    // If 401 Unauthorized and we haven't already retried this request
    if (response.status === 401 && !request.headers.has("X-Retried-401")) {
      const newToken = await refreshAccessToken();
      if (newToken) {
        // Clone request headers, set new token and retry marker
        const retryHeaders = new Headers(request.headers);
        retryHeaders.set("Authorization", `Bearer ${newToken}`);
        retryHeaders.set("X-Retried-401", "1");

        // Clone and re-send request
        const retryRequest = new Request(request, {
          headers: retryHeaders,
        });
        return fetch(retryRequest);
      }
    }

    return response;
  },
});

/**
 * Standard fetch wrapper that automatically attaches the in-memory Authorization header,
 * X-Request-ID, credentials: "include", and retries once on 401 via refreshAccessToken.
 */
export async function authFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const token = getAccessToken();
  const headers = new Headers(init?.headers);
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const requestId =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `req-${Date.now()}`;
  if (!headers.has("X-Request-ID")) {
    headers.set("X-Request-ID", requestId);
  }

  const reqInit: RequestInit = {
    ...init,
    headers,
    credentials: init?.credentials || "include",
  };

  let response = await fetch(input, reqInit);
  if (response.status === 401) {
    const newToken = await refreshAccessToken();
    if (newToken) {
      headers.set("Authorization", `Bearer ${newToken}`);
      response = await fetch(input, { ...reqInit, headers });
    }
  }
  return response;
}

