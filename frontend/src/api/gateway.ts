export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status = 0,
    readonly details: unknown = null,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

export interface JsonRequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Hard limit for the complete request, including response-body parsing. */
  timeoutMs?: number;
}

const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

async function readJsonResponse<T>(response: Response): Promise<T> {
  let payload: unknown = null;
  if (response.status !== 204) {
    try {
      payload = await response.json();
    } catch (error) {
      if (response.ok) {
        throw new GatewayError(
          "本地服务返回了无法识别的数据。",
          response.status,
          error,
        );
      }
    }
  }
  if (!response.ok) {
    const body = payload as {
      error?: { message?: string; code?: string };
      message?: string;
      detail?: string;
    } | null;
    throw new GatewayError(
      body?.error?.message ??
        body?.error?.code ??
        body?.detail ??
        body?.message ??
        `本地服务请求失败（HTTP ${response.status}）。`,
      response.status,
      payload,
    );
  }
  return (payload ?? {}) as T;
}

export async function requestJson<T>(
  path: string,
  options: JsonRequestOptions = {},
): Promise<T> {
  const requestedTimeout = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  const timeoutMs = Number.isFinite(requestedTimeout)
    ? Math.max(1, Math.trunc(requestedTimeout))
    : DEFAULT_REQUEST_TIMEOUT_MS;
  const controller = new AbortController();
  let timedOut = false;
  let timeoutHandle: number | null = null;
  let rejectCancellation: ((reason: DOMException) => void) | null = null;
  const cancellation = new Promise<never>((_resolve, reject) => {
    rejectCancellation = reject;
  });
  const forwardAbort = (): void => {
    controller.abort();
    rejectCancellation?.(new DOMException("操作已取消。", "AbortError"));
  };

  if (options.signal?.aborted) forwardAbort();
  else options.signal?.addEventListener("abort", forwardAbort, { once: true });

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...options.headers,
  };
  const init: RequestInit = {
    method: options.method ?? "GET",
    headers,
    signal: controller.signal,
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }

  try {
    const timeout = new Promise<never>((_resolve, reject) => {
      timeoutHandle = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
        reject(
          new GatewayError(
            `本地服务在 ${Math.ceil(timeoutMs / 1000)} 秒内没有响应，请重试。`,
            408,
          ),
        );
      }, timeoutMs);
    });
    const request = fetch(path, init).then((response) => readJsonResponse<T>(response));
    return await Promise.race([request, timeout, cancellation]);
  } catch (error) {
    if (timedOut) {
      if (error instanceof GatewayError && error.status === 408) throw error;
      throw new GatewayError(
        `本地服务在 ${Math.ceil(timeoutMs / 1000)} 秒内没有响应，请重试。`,
        408,
        error,
      );
    }
    if (options.signal?.aborted || isAbortError(error)) throw error;
    if (error instanceof GatewayError) throw error;
    throw new GatewayError("无法连接本地服务，请确认后端已经就绪。", 0, error);
  } finally {
    if (timeoutHandle !== null) window.clearTimeout(timeoutHandle);
    options.signal?.removeEventListener("abort", forwardAbort);
    rejectCancellation = null;
  }
}
