import { requestJson } from "../../api/gateway";
import type {
  RecommendationAction,
  RecommendationApi,
  RecommendationBatchWire,
  RecommendationEventResponse,
} from "./types";

export { GatewayError as RecommendationApiError } from "../../api/gateway";

export const recommendationApi: RecommendationApi = {
  create: (requestId, signal) =>
    requestJson<RecommendationBatchWire>("api/recommendations", {
      method: "POST",
      body: { request_id: requestId },
      signal,
    }),
  shown: (batchId, eventId, signal) =>
    requestJson<RecommendationEventResponse>(
      `api/recommendations/${encodeURIComponent(batchId)}/shown`,
      {
        method: "POST",
        body: { event_id: eventId },
        signal,
      },
    ),
  action: (
    batchId: string,
    eventId: string,
    itemId: string,
    action: RecommendationAction,
    signal?: AbortSignal,
  ) =>
    requestJson<RecommendationEventResponse>(
      `api/recommendations/${encodeURIComponent(batchId)}/actions`,
      {
        method: "POST",
        body: {
          event_id: eventId,
          item_id: itemId,
          action,
        },
        signal,
      },
    ),
};
