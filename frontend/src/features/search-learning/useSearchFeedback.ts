import { computed, ref, shallowReactive } from "vue";

import type { SearchResultItem } from "../../types/contracts";
import { searchLearningApi } from "./api";
import {
  canRecordFeedback,
  feedbackKey,
  type FeedbackAction,
  type SearchFeedbackEvent,
  type SearchLearningApi,
} from "./types";

export function useSearchFeedback(api: SearchLearningApi = searchLearningApi) {
  const events = shallowReactive(new Map<string, SearchFeedbackEvent>());
  const pendingKeys = shallowReactive(new Set<string>());
  const loadedSessions = new Set<string>();
  const loadingSessions = new Map<string, Promise<boolean>>();
  const enabled = ref(false);
  const implicitEnabled = ref(false);
  const lastError = ref("");

  const pending = computed(() => pendingKeys.size > 0);

  function feedbackFor(item: SearchResultItem | null): SearchFeedbackEvent | null {
    return item ? events.get(feedbackKey(item)) ?? null : null;
  }

  function isPending(item: SearchResultItem | null): boolean {
    return item ? pendingKeys.has(feedbackKey(item)) : false;
  }

  async function record(
    item: SearchResultItem,
    action: FeedbackAction,
    source = "context_menu",
  ): Promise<boolean> {
    if (!enabled.value) {
      lastError.value = "搜索学习未启用，请在设置中开启。";
      return false;
    }
    if (!canRecordFeedback(item)) {
      lastError.value = "当前搜索结果不支持反馈记录（缺少会话信息）。";
      return false;
    }
    if (!["relevant", "not_relevant"].includes(action) && !implicitEnabled.value) {
      return false;
    }
    const key = feedbackKey(item);
    if (pendingKeys.has(key)) return false;
    pendingKeys.add(key);
    lastError.value = "";
    try {
      const result = await api.feedback({
        session_id: item.searchSessionId,
        library_id: item.libraryId,
        doc_id: item.docId,
        action,
        source,
      });
      if ("accepted" in result && result.accepted === false) return false;
      // Only explicit judgements drive the visible relevant/not-relevant state.
      // Implicit events (open/copy/export/detail) may coexist for a candidate
      // and must not replace an explicit judgement in the context menu.
      if (action === "relevant" || action === "not_relevant") {
        events.set(key, result as SearchFeedbackEvent);
      }
      return true;
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error);
      return false;
    } finally {
      pendingKeys.delete(key);
    }
  }

  async function undo(item: SearchResultItem): Promise<boolean> {
    const key = feedbackKey(item);
    const current = events.get(key);
    if (!current || pendingKeys.has(key)) return false;
    pendingKeys.add(key);
    lastError.value = "";
    try {
      await api.revoke(current.event_id);
      events.delete(key);
      return true;
    } catch (error) {
      lastError.value = error instanceof Error ? error.message : String(error);
      return false;
    } finally {
      pendingKeys.delete(key);
    }
  }

  async function fetchSession(sessionId: string): Promise<boolean> {
    const restored = new Map<string, SearchFeedbackEvent>();
    const seenCursors = new Set<string>();
    let cursor = "";
    try {
      do {
        if (cursor && seenCursors.has(cursor)) {
          throw new Error("搜索反馈分页游标重复，已停止读取。");
        }
        if (cursor) seenCursors.add(cursor);
        const page = await api.listFeedback(sessionId, cursor || undefined);
        page.items.forEach((event) => {
          if (
            event.active &&
            (event.action === "relevant" || event.action === "not_relevant")
          ) {
            const key = `${event.session_id}\u0000${event.library_id}\u0000${event.doc_id}`;
            // The API returns newest first. Keep the first explicit event for
            // each candidate instead of letting an older page overwrite it.
            if (!restored.has(key)) restored.set(key, event);
          }
        });
        cursor = page.next_cursor ?? "";
      } while (cursor);

      for (const key of [...events.keys()]) {
        if (key.startsWith(`${sessionId}\u0000`)) events.delete(key);
      }
      restored.forEach((event, key) => events.set(key, event));
      loadedSessions.add(sessionId);
      lastError.value = "";
      return true;
    } catch (error) {
      // Feedback learning is optional. A broken/locked learning database must
      // never make the search result page unusable.
      lastError.value = error instanceof Error ? error.message : String(error);
      return false;
    }
  }

  async function loadSession(sessionId: string, force = false): Promise<boolean> {
    if (!sessionId || sessionId === "latest") return false;
    if (!force && loadedSessions.has(sessionId)) return true;
    const existing = loadingSessions.get(sessionId);
    if (existing) return existing;
    const request = fetchSession(sessionId).finally(() => {
      loadingSessions.delete(sessionId);
    });
    loadingSessions.set(sessionId, request);
    return request;
  }

  async function initialize(): Promise<void> {
    try {
      const status = await api.status();
      enabled.value = status.settings.learning_enabled;
      implicitEnabled.value = status.settings.implicit_feedback_enabled;
    } catch (error) {
      // Search remains fully usable when the optional learning store is down.
      lastError.value = error instanceof Error ? error.message : String(error);
    }
  }

  return {
    events,
    pending,
    enabled,
    implicitEnabled,
    lastError,
    feedbackFor,
    isPending,
    initialize,
    loadSession,
    markRelevant: (item: SearchResultItem) => record(item, "relevant"),
    markNotRelevant: (item: SearchResultItem) => record(item, "not_relevant"),
    recordImplicit: (
      item: SearchResultItem,
      action: Extract<FeedbackAction, "export" | "copy" | "open" | "detail">,
      source = "gallery",
    ) => record(item, action, source),
    undo,
  };
}
