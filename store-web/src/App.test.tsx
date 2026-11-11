import { describe, expect, it } from "vitest";
import { apiClient } from "./api/client";
import { LoginPage } from "./App";

describe("Web skeleton test suite", () => {
  it("initializes apiClient with GET method", () => {
    expect(typeof apiClient.GET).toBe("function");
  });

  it("exports LoginPage component", () => {
    expect(typeof LoginPage).toBe("function");
  });
});

