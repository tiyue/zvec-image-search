import { afterEach, describe, expect, it, vi } from "vitest";

import { recommendationApi } from "../api";

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("recommendationApi", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses the shared create, shown, and action contracts", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);

    await recommendationApi.create("request-1");
    await recommendationApi.shown("batch/1", "shown-1");
    await recommendationApi.action("batch/1", "action-1", "item-1", "like");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("api/recommendations");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      request_id: "request-1",
    });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("api/recommendations/batch%2F1/shown");
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      event_id: "shown-1",
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe("api/recommendations/batch%2F1/actions");
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      event_id: "action-1",
      item_id: "item-1",
      action: "like",
    });
  });
});
