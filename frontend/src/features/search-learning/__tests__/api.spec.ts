import { afterEach, describe, expect, it, vi } from "vitest";

import { searchLearningApi } from "../api";

describe("search-learning API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the selected fixed evaluation path to the dedicated import route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          installed: true,
          evaluation_set_id: "human-reviewed-v1",
          external_api_calls: 0,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const sourcePath = "C:\\Evaluation\\fixed-evaluation.json";

    await expect(searchLearningApi.installEvaluation(sourcePath)).resolves.toMatchObject({
      installed: true,
      external_api_calls: 0,
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      "api/search-learning/fixed-evaluation",
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(request.method).toBe("POST");
    expect(JSON.parse(String(request.body))).toEqual({ source_path: sourcePath });
  });
});
