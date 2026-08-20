import { afterEach, describe, expect, it, vi } from "vitest";

import {
  activityApi,
  activityLogQueryString,
  jobHistoryQueryString,
} from "../api";

function jsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: vi.fn(async () => payload),
  } as unknown as Response;
}

describe("activity API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("builds bounded cursor queries and omits empty filters", () => {
    expect(
      jobHistoryQueryString({
        status: " running ",
        taskType: "auto_tag",
        libraryId: "people",
        query: "失败",
        cursor: "next:1",
        limit: 999,
      }),
    ).toBe(
      "limit=200&cursor=next%3A1&status=running&task_type=auto_tag&library_id=people&query=%E5%A4%B1%E8%B4%A5",
    );
    expect(
      activityLogQueryString({
        level: "warning",
        category: "image_failure",
        libraryId: "",
        jobId: "job-1",
        query: "",
      }),
    ).toBe("limit=50&level=warning&category=image_failure&job_id=job-1");
  });

  it("uses the dedicated history and log endpoints", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ items: [] }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await activityApi.listJobHistory({
      status: "",
      taskType: "",
      libraryId: "",
      query: "",
    });
    await activityApi.listActivityLogs({
      level: "",
      category: "",
      libraryId: "",
      jobId: "",
      query: "",
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe("api/job-history?limit=50");
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe("api/activity-logs?limit=50");
  });
});
