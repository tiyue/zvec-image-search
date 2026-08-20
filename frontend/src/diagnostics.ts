export type DiagnosticLevel = "info" | "warning" | "error";

const DIAGNOSTIC_ENDPOINT = "api/diagnostics/frontend";
const DIAGNOSTIC_TIMEOUT_MS = 2_500;
const MAX_TEXT_LENGTH = 1_200;
const MAX_DETAIL_TEXT_LENGTH = 500;

function clippedText(value: unknown, fallback = "", maximum = MAX_TEXT_LENGTH): string {
  const text = typeof value === "string" ? value.trim() : fallback;
  return text
    .replace(/([?&](?:token|api[_-]?key|key)=)[^&\s]+/giu, "$1<redacted>")
    .replace(/\bsk-[a-zA-Z0-9_-]{12,}\b/gu, "<redacted>")
    .replace(/[\r\n\t]+/gu, " ")
    .slice(0, maximum);
}

function safeDetails(details: Record<string, unknown>): Record<string, unknown> {
  const safe: Record<string, unknown> = {};
  for (const [rawKey, value] of Object.entries(details).slice(0, 20)) {
    const key = rawKey.replace(/[^a-zA-Z0-9_.-]/gu, "_").slice(0, 64);
    if (!key) continue;
    if (typeof value === "string") safe[key] = clippedText(value, "", MAX_DETAIL_TEXT_LENGTH);
    else if (typeof value === "number" && Number.isFinite(value)) safe[key] = value;
    else if (typeof value === "boolean" || value === null) safe[key] = value;
  }
  return safe;
}

function errorDetails(value: unknown): Record<string, unknown> {
  if (value instanceof Error) {
    return {
      error_name: clippedText(value.name, "Error"),
      error_message: clippedText(value.message, "未知前端错误"),
      stack: clippedText(value.stack),
    };
  }
  return { error_name: "UnknownError", error_message: clippedText(String(value)) };
}

/**
 * Send a bounded, non-sensitive event to the local host. Logging is best-effort
 * and deliberately bypasses the normal API client so a gateway failure cannot
 * recursively create more diagnostics.
 */
export async function sendFrontendDiagnostic(
  event: string,
  details: Record<string, unknown> = {},
  level: DiagnosticLevel = "info",
  message = "",
): Promise<boolean> {
  const controller = new AbortController();
  const timeoutHandle = window.setTimeout(() => controller.abort(), DIAGNOSTIC_TIMEOUT_MS);
  try {
    const safeMessage = clippedText(message);
    const normalizedDetails = safeDetails(details);
    if (!safeMessage && Object.keys(normalizedDetails).length === 0) return false;
    const payload: Record<string, unknown> = {
      event: event.replace(/[^a-zA-Z0-9_.-]/gu, "_").slice(0, 80),
      level,
      details: normalizedDetails,
      timestamp: new Date().toISOString(),
    };
    if (safeMessage) payload.message = safeMessage;
    const response = await fetch(DIAGNOSTIC_ENDPOINT, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
      keepalive: true,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timeoutHandle);
  }
}

export function reportFrontendDiagnostic(
  event: string,
  details: Record<string, unknown> = {},
  level: DiagnosticLevel = "info",
  message = "",
): void {
  void sendFrontendDiagnostic(event, details, level, message);
}

export function installGlobalDiagnostics(): () => void {
  const handleWindowError = (event: ErrorEvent): void => {
    reportFrontendDiagnostic(
      "window_error",
      {
        ...errorDetails(event.error ?? event.message),
        source: clippedText(event.filename),
        line: event.lineno,
        column: event.colno,
      },
      "error",
      event.message,
    );
  };
  const handleUnhandledRejection = (event: PromiseRejectionEvent): void => {
    const details = errorDetails(event.reason);
    reportFrontendDiagnostic(
      "unhandled_rejection",
      details,
      "error",
      clippedText(details.error_message),
    );
  };

  window.addEventListener("error", handleWindowError);
  window.addEventListener("unhandledrejection", handleUnhandledRejection);
  return () => {
    window.removeEventListener("error", handleWindowError);
    window.removeEventListener("unhandledrejection", handleUnhandledRejection);
  };
}
