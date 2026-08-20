import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { requestJson } from "../gateway";

describe("requestJson", () => {
  beforeEach(() => vi.useFakeTimers());

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("returns JSON and forwards a caller signal through a linked controller", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();

    await expect(requestJson<{ ok: boolean }>("api/test", { signal: controller.signal })).resolves.toEqual({
      ok: true,
    });
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.signal).toBeInstanceOf(AbortSignal);
    expect(request.signal).not.toBe(controller.signal);
  });

  it("turns a permanently pending request into a clear timeout error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );

    const pending = requestJson("api/hangs", { timeoutMs: 25 });
    const assertion = expect(pending).rejects.toMatchObject({
      name: "GatewayError",
      status: 408,
    });
    await vi.advanceTimersByTimeAsync(25);
    await assertion;
  });

  it("preserves explicit cancellation instead of misreporting it as a timeout", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => undefined)),
    );
    const controller = new AbortController();
    const pending = requestJson("api/cancelled", {
      signal: controller.signal,
      timeoutMs: 5_000,
    });
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" });

    controller.abort();
    await assertion;
  });
});
