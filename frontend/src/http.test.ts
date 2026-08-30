import { describe, it, expect } from "vitest";
import { expectJson } from "./http";

function mockResponse(init: { ok: boolean; statusText: string; body?: unknown }): Response {
  const { ok, statusText, body } = init;
  if (body === undefined) {
    return {
      ok,
      statusText,
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
    } as Response;
  }
  return {
    ok,
    statusText,
    json: async () => body,
  } as Response;
}

describe("expectJson", () => {
  it("returns parsed JSON on success", async () => {
    const resp = mockResponse({ ok: true, statusText: "OK", body: { order_id: "abc" } });
    expect(await expectJson<{ order_id: string }>(resp)).toEqual({ order_id: "abc" });
  });

  it("throws with [code] message format on error envelope", async () => {
    const resp = mockResponse({
      ok: false,
      statusText: "Internal Server Error",
      body: { error: { code: "INVALID_QTY", message: "quantity must be > 0" } },
    });
    await expect(expectJson(resp)).rejects.toThrow("[INVALID_QTY] quantity must be > 0");
  });

  it("falls back to statusText on non-JSON error body", async () => {
    const resp = mockResponse({ ok: false, statusText: "Bad Request" });
    await expect(expectJson(resp)).rejects.toThrow("Bad Request");
  });
});
