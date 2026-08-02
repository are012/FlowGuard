import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, useRemote } from "@/lib/api";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

describe("useRemote", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("reuses module-cached data when the same path mounts again", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ balance: 120_000 }));

    const first = renderHook(() => useRemote<{ balance: number }>("/cache/reuse"));
    expect(first.result.current.loading).toBe(true);
    await waitFor(() => expect(first.result.current.data).toEqual({ balance: 120_000 }));
    first.unmount();

    const second = renderHook(() => useRemote<{ balance: number }>("/cache/reuse"));
    expect(second.result.current).toMatchObject({
      data: { balance: 120_000 },
      loading: false,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("forces a network refresh when reload is requested explicitly", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ version: 1 }))
      .mockResolvedValueOnce(jsonResponse({ version: 2 }));

    const hook = renderHook(() => useRemote<{ version: number }>("/cache/reload"));
    await waitFor(() => expect(hook.result.current.data).toEqual({ version: 1 }));

    act(() => hook.result.current.reload());
    expect(hook.result.current.loading).toBe(true);
    await waitFor(() => expect(hook.result.current.data).toEqual({ version: 2 }));

    expect(hook.result.current.loading).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("invalidates cached GET data after a successful mutation", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ version: 1 }))
      .mockResolvedValueOnce(jsonResponse({ updated: true }))
      .mockResolvedValueOnce(jsonResponse({ version: 2 }));

    const first = renderHook(() => useRemote<{ version: number }>("/cache/mutation"));
    await waitFor(() => expect(first.result.current.data).toEqual({ version: 1 }));
    first.unmount();

    await apiRequest("/mutation", { method: "POST", body: "{}" });

    const second = renderHook(() => useRemote<{ version: number }>("/cache/mutation"));
    expect(second.result.current.loading).toBe(true);
    await waitFor(() => expect(second.result.current.data).toEqual({ version: 2 }));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not cache a GET response that started before a mutation", async () => {
    let resolveStaleRequest: (response: Response) => void = () => undefined;
    const staleRequest = new Promise<Response>((resolve) => {
      resolveStaleRequest = resolve;
    });
    fetchMock
      .mockImplementationOnce(() => staleRequest)
      .mockResolvedValueOnce(jsonResponse({ updated: true }))
      .mockResolvedValueOnce(jsonResponse({ version: 2 }));

    const hook = renderHook(() => useRemote<{ version: number }>("/cache/race"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    await apiRequest("/mutation", { method: "POST", body: "{}" });
    resolveStaleRequest(jsonResponse({ version: 1 }));

    await waitFor(() => expect(hook.result.current.data).toEqual({ version: 2 }));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("retries a stale GET failure that arrives after a mutation", async () => {
    let rejectStaleRequest: (error: Error) => void = () => undefined;
    const staleRequest = new Promise<Response>((_resolve, reject) => {
      rejectStaleRequest = reject;
    });
    fetchMock
      .mockImplementationOnce(() => staleRequest)
      .mockResolvedValueOnce(jsonResponse({ updated: true }))
      .mockResolvedValueOnce(jsonResponse({ version: 2 }));

    const hook = renderHook(() => useRemote<{ version: number }>("/cache/race-error"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    await apiRequest("/mutation", { method: "POST", body: "{}" });
    rejectStaleRequest(new Error("stale request failed"));

    await waitFor(() => expect(hook.result.current.data).toEqual({ version: 2 }));
    expect(hook.result.current.error).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
