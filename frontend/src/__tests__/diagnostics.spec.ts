import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installGlobalDiagnostics, sendFrontendDiagnostic } from "../diagnostics";

describe("frontend diagnostics", () => {
  beforeEach(() => vi.useFakeTimers());

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("posts a bounded event and removes local tokens before persistence", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      sendFrontendDiagnostic(
        "search_failed",
        { source: "http://127.0.0.1/app.js?token=secret-value", attempt: 2 },
        "error",
        "请求失败",
      ),
    ).resolves.toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("api/diagnostics/frontend");
    const payload = JSON.parse(String(init.body)) as {
      event: string;
      details: Record<string, unknown>;
    };
    expect(payload.event).toBe("search_failed");
    expect(payload.details.source).toContain("<redacted>");
    expect(JSON.stringify(payload)).not.toContain("secret-value");
  });

  it("never blocks the UI when the diagnostic endpoint hangs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener(
            "abort",
            () => reject(new DOMException("aborted", "AbortError")),
            { once: true },
          );
        }),
      ),
    );

    const pending = sendFrontendDiagnostic(
      "search_poll_started",
      { stage: "search" },
      "warning",
    );
    const assertion = expect(pending).resolves.toBe(false);
    await vi.advanceTimersByTimeAsync(2_500);
    await assertion;
  });

  it("omits an empty message so detail-only events match the server contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      sendFrontendDiagnostic("search_completed", { total_items: 15 }),
    ).resolves.toBe(true);

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(payload).not.toHaveProperty("message");
    expect(payload.details).toEqual({ total_items: 15 });
  });

  it("captures an unhandled rejection and detaches cleanly", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const uninstall = installGlobalDiagnostics();

    window.dispatchEvent(
      new PromiseRejectionEvent("unhandledrejection", {
        promise: Promise.resolve(),
        reason: new Error("测试异常"),
      }),
    );
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    uninstall();
    window.dispatchEvent(new ErrorEvent("error", { message: "卸载后错误" }));
    await Promise.resolve();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
