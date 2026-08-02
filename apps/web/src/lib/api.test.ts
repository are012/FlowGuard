import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { API_BASE_URL, ApiError, apiRequest, listFrom } from "@/lib/api";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("apiRequest", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("normalizes the path and sends JSON requests without browser caching", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));

    await expect(
      apiRequest<{ ok: boolean }>("health", {
        method: "POST",
        body: JSON.stringify({ check: true }),
      }),
    ).resolves.toEqual({ ok: true });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API_BASE_URL}/health`);
    expect(init?.cache).toBe("no-store");
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
  });

  it("does not set a multipart content type for FormData", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ imported: true }));
    const body = new FormData();
    body.append("file", new Blob(["date,amount"]), "cashflow.csv");

    await apiRequest("/imports", { method: "POST", body });

    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
  });

  it("returns undefined for a successful no-content response", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(apiRequest<void>("/resource", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it.each([
    [{ detail: "계좌를 찾을 수 없습니다." }, "계좌를 찾을 수 없습니다."],
    [
      { detail: [{ msg: "날짜를 확인하세요." }, {}, { msg: "금액을 확인하세요." }] },
      "날짜를 확인하세요., 금액을 확인하세요.",
    ],
    [{ message: "서버 처리에 실패했습니다." }, "서버 처리에 실패했습니다."],
  ])("uses a structured API error message", async (body, message) => {
    fetchMock.mockResolvedValue(jsonResponse(body, { status: 422, statusText: "Unprocessable" }));

    const promise = apiRequest("/failure");

    await expect(promise).rejects.toEqual(new ApiError(message, 422));
  });

  it("falls back to the HTTP status text when an error body is invalid", async () => {
    fetchMock.mockResolvedValue(
      new Response("not-json", { status: 500, statusText: "Internal Server Error" }),
    );

    await expect(apiRequest("/failure")).rejects.toMatchObject({
      name: "ApiError",
      message: "Internal Server Error",
      status: 500,
    });
  });

  it("uses the generic fallback when structured errors contain no message", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: [{}] }, { status: 400 }));

    await expect(apiRequest("/failure")).rejects.toMatchObject({
      message: "요청을 처리하지 못했습니다.",
      status: 400,
    });
  });
});

describe("listFrom", () => {
  it("accepts arrays and supported list envelopes", () => {
    expect(listFrom([1, 2])).toEqual([1, 2]);
    expect(listFrom({ items: [3] })).toEqual([3]);
    expect(listFrom({ data: [4] })).toEqual([4]);
    expect(listFrom(undefined)).toEqual([]);
  });
});
