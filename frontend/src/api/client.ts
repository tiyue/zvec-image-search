import type {
  BootstrapResponse,
  SearchHistoryResponse,
  SearchPageResponse,
  SearchSubmission,
} from "../types/contracts";
import { requestJson } from "./gateway";

export { GatewayError as ApiError } from "./gateway";

export interface SearchApi {
  bootstrap(signal?: AbortSignal): Promise<BootstrapResponse>;
  latest(page: number, pageSize: 15, signal?: AbortSignal): Promise<SearchPageResponse>;
  submit(body: SearchSubmission, signal?: AbortSignal): Promise<SearchPageResponse>;
  page(
    operationId: string,
    page: number,
    pageSize: 15,
    signal?: AbortSignal,
  ): Promise<SearchPageResponse>;
  cancel(operationId: string): Promise<SearchPageResponse>;
  history?(signal?: AbortSignal): Promise<SearchHistoryResponse>;
  historyPage?(
    historyId: string,
    page: number,
    pageSize: 15,
    signal?: AbortSignal,
  ): Promise<SearchPageResponse>;
}

export const searchApi: SearchApi = {
  bootstrap: (signal) => requestJson<BootstrapResponse>("api/bootstrap", { signal }),
  latest: (page, pageSize, signal) =>
    requestJson<SearchPageResponse>(
      `api/results/latest?page=${page}&page_size=${pageSize}`,
      { signal },
    ),
  submit: (body, signal) =>
    requestJson<SearchPageResponse>("api/search", {
      method: "POST",
      body,
      signal,
    }),
  page: (operationId, page, pageSize, signal) =>
    requestJson<SearchPageResponse>(
      `api/search/${encodeURIComponent(operationId)}?page=${page}&page_size=${pageSize}`,
      { signal },
    ),
  cancel: (operationId) =>
    requestJson<SearchPageResponse>(`api/search/${encodeURIComponent(operationId)}`, {
      method: "DELETE",
    }),
  history: (signal) =>
    requestJson<SearchHistoryResponse>("api/results/history?limit=12", { signal }),
  historyPage: (historyId, page, pageSize, signal) =>
    requestJson<SearchPageResponse>(
      `api/results/history/${encodeURIComponent(historyId)}?page=${page}&page_size=${pageSize}`,
      { signal },
    ),
};
