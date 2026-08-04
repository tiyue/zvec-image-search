import { afterEach, describe, expect, it, vi } from "vitest";

import {
  cancelJob,
  cancelProjectWork,
  exportFiles,
  getJob,
  getSourceStatus,
  importFiles,
  importFolder,
  listMembers,
  saveWorkspaceState,
  updateCreativeLook,
  updateRating,
} from "../api";

function response(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("raw-selection api", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends multi-file imports and every explicit list query without leaking them into a path", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({
        registered: 1,
        skipped_unsupported: 0,
        skipped_raw_formats: 0,
        skipped_unsupported_camera: 1,
        skipped_reparse: 0,
        errors: 0,
        error_details: [],
        unsupported_camera_details: ["D:\\B.ARW: ILCE-7M3"],
      }))
      .mockResolvedValueOnce(response({ members: [], total: 0, filtered: 0 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await importFiles("project/1", ["D:\\A.ARW", "D:\\B.JPG"]);
    await listMembers("project/1", {
      formats: "arw,jpeg",
      orientations: "landscape",
      exported: "unexported",
      sort: "mtime_ns",
      dir: "desc",
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe("api/raw-selection/projects/project/1/import-files");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      paths: ["D:\\A.ARW", "D:\\B.JPG"],
    });
    expect(fetchMock.mock.calls[1]?.[0]).toContain("formats=arw%2Cjpeg");
    expect(fetchMock.mock.calls[1]?.[0]).toContain("orientations=landscape");
    expect(fetchMock.mock.calls[1]?.[0]).toContain("exported=unexported");
    expect(result).toMatchObject({
      skipped_unsupported_camera: 1,
      unsupported_camera_details: ["D:\\B.ARW: ILCE-7M3"],
    });
  });

  it("starts import/export jobs and exposes polling, cancellation and source diagnostics", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);

    await importFolder("project-1", "D:\\wedding");
    await exportFiles("project-1", ["member-1"], "D:\\export");
    await getJob("job-1");
    await cancelJob("job-1");
    await getSourceStatus("member-1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "api/raw-selection/projects/project-1/import-folder",
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      path: "D:\\wedding",
    });
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "api/raw-selection/projects/project-1/export",
    );
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      member_ids: ["member-1"],
      destination: "D:\\export",
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe("api/raw-selection/jobs/job-1");
    expect(fetchMock.mock.calls[3]?.[0]).toBe("api/raw-selection/jobs/job-1/cancel");
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe("POST");
    expect(fetchMock.mock.calls[4]?.[0]).toBe(
      "api/raw-selection/members/member-1/source-status",
    );
  });

  it("keeps PATCH mutations and persists all new workspace fields", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);
    await updateRating("member-1", 4, "red");
    await updateCreativeLook("member-1", "ST");
    await saveWorkspaceState("project-1", {
      filter_exported: "unexported",
      filter_formats: "arw,png",
      filter_orientations: "portrait",
    });
    await cancelProjectWork("project-1");

    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe("PATCH");
    expect(fetchMock.mock.calls[1]?.[1]?.method).toBe("PATCH");
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toMatchObject({
      filter_exported: "unexported",
      filter_formats: "arw,png",
      filter_orientations: "portrait",
    });
    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      "api/raw-selection/projects/project-1/cancel-work",
    );
    expect(fetchMock.mock.calls[3]?.[1]?.method).toBe("POST");
  });
});
