import { describe, expect, it, vi } from "vitest";

import type { SearchResultItem } from "../../../types/contracts";
import type {
  SearchFeedbackEvent,
  SearchLearningApi,
  SearchLearningStatus,
} from "../types";
import { useSearchFeedback } from "../useSearchFeedback";

const item: SearchResultItem = {
  id: "image-1",
  searchSessionId: "search-1",
  libraryId: "lib-1",
  docId: "doc-1",
  sha256: "a".repeat(64),
  name: "image.jpg",
  relativePath: "folder/image.jpg",
  libraryName: "Library",
  rank: 1,
  matchState: "high",
  rankSource: "text",
  width: 100,
  height: 100,
  tags: [],
  matchedTags: [],
  confidence: 0.8,
  rawScore: 0.2,
  normalizedScore: 0.8,
  sizeBytes: 100,
  imageUrl: "",
  thumbnailUrl: "",
};

const status: SearchLearningStatus = {
  available: true,
  database: "search-learning.sqlite3",
  online_weight_updates: false,
  fixed_evaluation_gate_required: true,
  training_running: false,
  settings: {
    learning_enabled: true,
    implicit_feedback_enabled: false,
    save_query_text: false,
    shadow_mode: true,
  },
  training_counts: {
    query_sessions: 0,
    explicit_samples: 0,
    positive_samples: 0,
    negative_samples: 0,
  },
  minimum_requirements: {
    query_sessions: 100,
    explicit_samples: 300,
    positive_and_negative_required: true,
  },
};

function event(action: "relevant" | "not_relevant" = "relevant"): SearchFeedbackEvent {
  return {
    event_id: `feedback-${action}`,
    session_id: item.searchSessionId,
    library_id: item.libraryId,
    doc_id: item.docId,
    action,
    feedback_weight: action === "relevant" ? 1 : -1,
    created_at: "2026-07-19T00:00:00Z",
    source: "context_menu",
    active: true,
  };
}

function fakeApi(): SearchLearningApi {
  return {
    status: vi.fn().mockResolvedValue(status),
    updateSettings: vi.fn().mockResolvedValue(status.settings),
    feedback: vi.fn().mockResolvedValue(event()),
    revoke: vi.fn().mockResolvedValue({ ...event(), active: false }),
    listFeedback: vi.fn().mockResolvedValue({
      items: [event()],
      has_more: false,
      next_cursor: null,
    }),
    train: vi.fn().mockResolvedValue({}),
    installEvaluation: vi.fn().mockResolvedValue({ installed: true }),
    activate: vi.fn().mockResolvedValue(status.settings),
    rollback: vi.fn().mockResolvedValue(status.settings),
    clear: vi.fn().mockResolvedValue({ cleared: true }),
    exportData: vi.fn().mockResolvedValue({ examples: [] }),
  };
}

describe("useSearchFeedback", () => {
  it("records explicit feedback with stable candidate identifiers and undoes it", async () => {
    const api = fakeApi();
    const feedback = useSearchFeedback(api);

    expect(await feedback.markRelevant(item)).toBe(true);
    expect(api.feedback).toHaveBeenCalledWith({
      session_id: "search-1",
      library_id: "lib-1",
      doc_id: "doc-1",
      action: "relevant",
      source: "context_menu",
    });
    expect(feedback.feedbackFor(item)?.action).toBe("relevant");

    expect(await feedback.undo(item)).toBe(true);
    expect(api.revoke).toHaveBeenCalledWith("feedback-relevant");
    expect(feedback.feedbackFor(item)).toBeNull();
  });

  it("does not record implicit actions until the user opts in", async () => {
    const api = fakeApi();
    const feedback = useSearchFeedback(api);
    await feedback.initialize();

    expect(await feedback.recordImplicit(item, "open")).toBe(false);
    expect(api.feedback).not.toHaveBeenCalled();

    feedback.implicitEnabled.value = true;
    expect(await feedback.recordImplicit(item, "open")).toBe(true);
  });

  it("fails open when learning is unavailable and keeps UI state reusable", async () => {
    const api = fakeApi();
    vi.mocked(api.feedback).mockRejectedValueOnce(new Error("disk full"));
    const feedback = useSearchFeedback(api);

    expect(await feedback.markNotRelevant(item)).toBe(false);
    expect(feedback.pending.value).toBe(false);
    expect(feedback.lastError.value).toBe("disk full");

    vi.mocked(api.feedback).mockResolvedValueOnce(event("not_relevant"));
    expect(await feedback.markNotRelevant(item)).toBe(true);
  });

  it("does not attach feedback to the untracked latest-results pseudo session", async () => {
    const api = fakeApi();
    const feedback = useSearchFeedback(api);
    const latestItem = { ...item, searchSessionId: "latest" };

    expect(await feedback.markRelevant(latestItem)).toBe(false);
    expect(api.feedback).not.toHaveBeenCalled();
  });

  it("restores persisted active feedback by session", async () => {
    const api = fakeApi();
    const feedback = useSearchFeedback(api);
    await feedback.loadSession("search-1");
    expect(feedback.feedbackFor(item)?.event_id).toBe("feedback-relevant");
  });

  it("keeps explicit menu state when an implicit action is recorded", async () => {
    const api = fakeApi();
    const feedback = useSearchFeedback(api);
    feedback.implicitEnabled.value = true;

    expect(await feedback.markRelevant(item)).toBe(true);
    vi.mocked(api.feedback).mockResolvedValueOnce({
      ...event(),
      event_id: "feedback-open",
      action: "open",
      feedback_weight: 0.2,
    });
    expect(await feedback.recordImplicit(item, "open")).toBe(true);

    expect(feedback.feedbackFor(item)?.action).toBe("relevant");
  });

  it("fails open while loading persisted feedback and permits a later retry", async () => {
    const api = fakeApi();
    vi.mocked(api.listFeedback)
      .mockRejectedValueOnce(new Error("learning database locked"))
      .mockResolvedValueOnce({
        items: [event("not_relevant")],
        has_more: false,
        next_cursor: null,
      });
    const feedback = useSearchFeedback(api);

    await expect(feedback.loadSession("search-1")).resolves.toBe(false);
    expect(feedback.lastError.value).toBe("learning database locked");
    await expect(feedback.loadSession("search-1")).resolves.toBe(true);
    expect(feedback.feedbackFor(item)?.action).toBe("not_relevant");
    expect(api.listFeedback).toHaveBeenCalledTimes(2);
  });

  it("restores only explicit judgements and keeps the newest one per candidate", async () => {
    const api = fakeApi();
    vi.mocked(api.listFeedback).mockResolvedValueOnce({
      items: [
        event("not_relevant"),
        { ...event(), event_id: "feedback-open", action: "open", feedback_weight: 0.2 },
        { ...event(), event_id: "feedback-older" },
      ],
      has_more: false,
      next_cursor: null,
    });
    const feedback = useSearchFeedback(api);

    expect(await feedback.loadSession("search-1")).toBe(true);
    expect(feedback.feedbackFor(item)?.event_id).toBe("feedback-not_relevant");
  });
});
