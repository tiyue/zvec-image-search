import { flushPromises, mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { RecommendationApi, RecommendationBatchWire } from "../types";
import { normalizeRecommendationBatch, useRecommendations } from "../useRecommendations";

function wireBatch(batchId: string, count = 15): RecommendationBatchWire {
  return {
    request_id: `request-${batchId}`,
    batch_id: batchId,
    count,
    partial: count < 15,
    partial_reason: count < 15 ? "insufficient_candidates" : null,
    quota_degraded: count < 15,
    history_window: 60,
    quota: { quality: 5, recent: 4, low_exposure: 4, random: 2 },
    diversity: {
      applied: true,
      reason: "",
      missing_vectors: 2,
      vector_space: "clip-test",
    },
    items: Array.from({ length: count }, (_, index) => ({
      item_id: `${batchId}-item-${index + 1}`,
      media_id: `${batchId}-media-${index + 1}`,
      name: `图片 ${index + 1}`,
      width: 1200,
      height: 800,
      tags: ["角色"],
      library_id: "library-1",
      library_name: "人物图库",
      content_type: "image/jpeg",
      size_bytes: 2048,
      bucket: index < 5 ? "quality" : index < 9 ? "recent" : index < 13 ? "low_exposure" : "random",
      thumbnail_url: `api/image/${batchId}-${index + 1}?variant=thumbnail`,
      preview_url: `api/image/${batchId}-${index + 1}?variant=preview`,
    })),
  };
}

function apiMock(first = wireBatch("batch-1")): RecommendationApi {
  return {
    create: vi.fn().mockResolvedValue(first),
    shown: vi.fn().mockResolvedValue({ ok: true }),
    action: vi.fn().mockResolvedValue({ ok: true }),
  };
}

function mountState(
  api: RecommendationApi,
  options: Parameters<typeof useRecommendations>[1] = {},
) {
  let state!: ReturnType<typeof useRecommendations>;
  const wrapper = mount(defineComponent({
    setup() {
      state = useRecommendations(api, options);
      return () => h("div");
    },
  }));
  return { wrapper, state };
}

describe("useRecommendations", () => {
  const wrappers: Array<{ unmount: () => void }> = [];

  afterEach(() => {
    wrappers.splice(0).forEach((wrapper) => wrapper.unmount());
  });

  it("loads only on first visibility, preloads all images, and reports shown once", async () => {
    const api = apiMock();
    const preloadImage = vi.fn().mockResolvedValue(undefined);
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage,
    });
    wrappers.push(mounted.wrapper);

    expect(api.create).not.toHaveBeenCalled();
    mounted.state.setVisible(true);
    await flushPromises();

    expect(api.create).toHaveBeenCalledTimes(1);
    expect(preloadImage).toHaveBeenCalledTimes(15);
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledWith("batch-1", "event-2", expect.any(AbortSignal));

    mounted.state.setVisible(false);
    mounted.state.setVisible(true);
    await flushPromises();
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("keeps the old batch until every new thumbnail is ready", async () => {
    const api = apiMock();
    let blocked = false;
    const releases: Array<() => void> = [];
    const preloadImage = vi.fn(() => blocked
      ? new Promise<void>((resolve) => releases.push(resolve))
      : Promise.resolve());
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage,
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");

    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-2"));
    blocked = true;
    const refresh = mounted.state.refresh();
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");

    releases.splice(0).forEach((release) => release());
    await refresh;
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-2");
    expect(api.shown).toHaveBeenCalledTimes(2);
  });

  it("does not replace or report a batch whose thumbnail preload fails", async () => {
    const api = apiMock();
    let fail = false;
    const preloadImage = vi.fn(() => fail
      ? Promise.reject(new Error("broken thumbnail"))
      : Promise.resolve());
    const onError = vi.fn();
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage,
      onError,
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();

    vi.mocked(api.create).mockResolvedValueOnce(wireBatch("batch-2"));
    fail = true;
    await expect(mounted.state.refresh()).resolves.toBe(false);

    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith("无法刷新推荐", "broken thumbnail");
  });

  it("holds a prepared batch while hidden and commits it when visible again", async () => {
    const api = apiMock();
    const releases: Array<() => void> = [];
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: () => new Promise<void>((resolve) => releases.push(resolve)),
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    mounted.state.setVisible(false);
    releases.splice(0).forEach((release) => release());
    await flushPromises();
    expect(mounted.state.currentBatch.value).toBeNull();
    expect(api.shown).not.toHaveBeenCalled();

    mounted.state.setVisible(true);
    await flushPromises();
    expect(mounted.state.currentBatch.value?.batchId).toBe("batch-1");
    expect(api.shown).toHaveBeenCalledTimes(1);
  });

  it("reuses event ids when shown or action writes are retried", async () => {
    const api = apiMock();
    vi.mocked(api.shown)
      .mockRejectedValueOnce(new Error("temporary shown failure"))
      .mockResolvedValueOnce({ ok: true });
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("temporary action failure"))
      .mockResolvedValueOnce({ ok: true });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();

    await mounted.state.retryShown();
    const shownIds = vi.mocked(api.shown).mock.calls.map((call) => call[1]);
    expect(shownIds).toEqual(["event-2", "event-2"]);

    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);
    const actionIds = vi.mocked(api.action).mock.calls.map((call) => call[1]);
    expect(actionIds).toEqual(["event-3", "event-3"]);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("starts a new preference event after the opposite action succeeds", async () => {
    const api = apiMock(wireBatch("preference-retry", 1));
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("like response was lost"))
      .mockResolvedValue({ ok: true });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "dislike")).resolves.toBe(true);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);

    expect(vi.mocked(api.action).mock.calls.map((call) => call[1])).toEqual([
      "event-3",
      "event-4",
      "event-5",
    ]);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("keeps the server-final preference when an old event id is replayed", async () => {
    const api = apiMock(wireBatch("cross-viewer-replay", 1));
    vi.mocked(api.action)
      .mockRejectedValueOnce(new Error("like was stored but its response was lost"))
      .mockResolvedValueOnce({
        ok: true,
        recorded: false,
        preference: "dislike",
      });
    let sequence = 0;
    const mounted = mountState(api, {
      idFactory: () => `event-${++sequence}`,
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(true);

    expect(vi.mocked(api.action).mock.calls.map((call) => call[1])).toEqual([
      "event-3",
      "event-3",
    ]);
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");
  });

  it("normalizes partial and vector-diversity status without exposing viewer identity", () => {
    const batch = normalizeRecommendationBatch(wireBatch("partial", 7));

    expect(batch.count).toBe(7);
    expect(batch.partial).toBe(true);
    expect(batch.partialReason).toBe("insufficient_candidates");
    expect(batch.quotaDegraded).toBe(true);
    expect(batch.diversity).toEqual({
      applied: true,
      reason: "",
      missingVectors: 2,
      vectorSpace: "clip-test",
    });
    expect(batch).not.toHaveProperty("viewerId");
  });

  it("normalizes the server-final preference and safe personalization status", () => {
    const wire = wireBatch("personalized", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "dislike" });
    Object.assign(wire, {
      personalization: {
        applied: true,
        effective_count: 14,
        reason: null,
      },
    });

    const batch = normalizeRecommendationBatch(wire);

    expect(batch.items[0]).toMatchObject({ preference: "dislike" });
    expect(batch.personalization).toEqual({
      applied: true,
      effectiveCount: 14,
      reason: "",
    });
  });

  it("restores a shared preference and preserves it when a replacement write fails", async () => {
    const wire = wireBatch("shared", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "dislike" });
    const api = apiMock(wire);
    vi.mocked(api.action).mockRejectedValueOnce(new Error("conflict write failed"));
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);

    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");

    await expect(mounted.state.recordAction(itemId, "like")).resolves.toBe(false);
    expect(mounted.state.preferenceFor(itemId)).toBe("dislike");
  });

  it("safely degrades invalid preference and personalization metadata", () => {
    const wire = wireBatch("invalid-personalization", 1);
    Object.assign(wire.items?.[0] ?? {}, { preference: "favorite" });
    Object.assign(wire, {
      personalization: {
        applied: false,
        effective_count: -4,
        reason: "viewer-secret-value",
      },
    });

    const batch = normalizeRecommendationBatch(wire);

    expect(batch.items[0]?.preference).toBeNull();
    expect(batch.personalization).toEqual({
      applied: false,
      effectiveCount: 0,
      reason: "",
    });
  });

  it("keeps the safe replay reason without inventing personalization state", () => {
    const wire = wireBatch("replayed", 1);
    Object.assign(wire, {
      personalization: {
        applied: false,
        effective_count: 0,
        reason: "replayed",
      },
    });

    expect(normalizeRecommendationBatch(wire).personalization).toEqual({
      applied: false,
      effectiveCount: 0,
      reason: "replayed",
    });
  });

  it("does not submit conflicting preference writes for one item concurrently", async () => {
    const api = apiMock(wireBatch("pending-preference", 1));
    let finish!: () => void;
    vi.mocked(api.action).mockImplementationOnce(() => new Promise((resolve) => {
      finish = () => resolve({ ok: true });
    }));
    const mounted = mountState(api, {
      idFactory: (() => {
        let sequence = 0;
        return () => `event-${++sequence}`;
      })(),
      preloadImage: vi.fn().mockResolvedValue(undefined),
    });
    wrappers.push(mounted.wrapper);
    mounted.state.setVisible(true);
    await flushPromises();
    const itemId = mounted.state.currentBatch.value?.items[0]?.itemId ?? "";

    const liking = mounted.state.recordAction(itemId, "like");
    await flushPromises();
    await expect(mounted.state.recordAction(itemId, "dislike")).resolves.toBe(false);
    expect(api.action).toHaveBeenCalledTimes(1);

    finish();
    await expect(liking).resolves.toBe(true);
    expect(mounted.state.preferenceFor(itemId)).toBe("like");
  });

  it("formats structured vector-space metadata", () => {
    const wire = wireBatch("structured-space");
    wire.diversity!.vector_space = {
      model: "embedding-v1",
      dimension: 1024,
      metric: "COSINE",
    };

    expect(normalizeRecommendationBatch(wire).diversity.vectorSpace).toBe(
      "embedding-v1 · 1024D · COSINE",
    );
  });
});
