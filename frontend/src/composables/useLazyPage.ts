import type { Component } from "vue";

/**
 * Page key to emitted page-chunk URL. The production build
 * (`vite.config.ts`) replaces this marker with the real chunk file names so a
 * manual retry can request the module again; in dev and tests the marker
 * stays unset and the retry falls back to the plain dynamic import.
 */
export const lazyPageChunkUrls = "__zvec_lazy_page_chunks__" as unknown as Record<string, string>;

/** Pages whose module load already failed once in this document. */
const failedPages = new Set<string>();

/**
 * Stylesheet URL Vite reported as failing to preload, per page. Vite's preload
 * helper leaves the failed `<link>` in the document and treats a matching href
 * as already loaded, so the module retry has to re-request that exact
 * stylesheet at a fresh URL before importing the module again.
 */
const failedPageCss = new Map<string, string>();

let retryNonce = 0;

/**
 * Read the stylesheet URL out of Vite's `Unable to preload CSS for <url>`
 * error. The build helper reports the resolved dependency URL; any other
 * failure (an ordinary module error) yields an empty string.
 */
export function cssUrlFromError(error: unknown): string {
  const message =
    error instanceof Error ? error.message : typeof error === "string" ? error : "";
  const match = /Unable to preload CSS for (\S+)/u.exec(message);
  return match ? match[1] : "";
}

/**
 * Build the URL for a retry of `key`: the emitted page chunk with a fresh
 * query. A failed dynamic import is cached in the browser's module map for the
 * lifetime of the document, so re-importing the original specifier can never
 * recover; a new URL does issue a real request again.
 */
export function lazyPageRetryUrl(
  key: string,
  base: string,
  nonce: number,
  urls: Record<string, string> = lazyPageChunkUrls,
): string | null {
  const relative = urls[key];
  if (!relative) return null;
  const target = new URL(relative, base);
  target.searchParams.set("zvecRetry", String(nonce));
  return target.href;
}

/**
 * Re-request an already-appended stylesheet `<link>` at a fresh URL, resolving
 * with whether the stylesheet loaded. Vite appends the failed link itself, so
 * the retry finds it by href instead of creating a second one; the direct
 * module import on retry would otherwise never ask for the CSS again. When no
 * matching link is found (dev, tests) the caller just proceeds.
 */
export function reloadPageStylesheet(url: string, nonce: number): Promise<boolean> {
  if (typeof document === "undefined") return Promise.resolve(true);
  const withoutQuery = (value: string) => value.split(/[?#]/u)[0];
  const links = Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]'));
  const found =
    links.find((item) => item.href === url) ??
    links.find((item) => withoutQuery(item.href) === withoutQuery(url));
  if (!found) return Promise.resolve(true);
  const link = found;
  const target = new URL(url, document.baseURI);
  target.searchParams.set("zvecCssRetry", String(nonce));
  return new Promise<boolean>((resolve) => {
    // Both handlers stay registered until one of them settles the promise, so
    // a manual retry has to drop the loser explicitly; otherwise every retry
    // would keep its closures alive on the shared <link>.
    const onLoad = () => finish(true);
    const onError = () => finish(false);
    function finish(loaded: boolean): void {
      link.removeEventListener("load", onLoad);
      link.removeEventListener("error", onError);
      resolve(loaded);
    }
    link.addEventListener("load", onLoad);
    link.addEventListener("error", onError);
    link.href = target.href;
  });
}

/**
 * Wrap a lazy page load so the first attempt is the direct `.vue` dynamic
 * import and a later attempt (the error surface's manual retry) requests the
 * emitted chunk at a fresh URL. Nothing here retries on its own.
 */
export function createLazyPageLoader<P extends Component>(
  key: string,
  direct: () => Promise<P>,
  options: {
    urls?: Record<string, string>;
    /** Indirection so the retry path stays testable without a network. */
    importer?: (url: string) => Promise<unknown>;
    /** Indirection so the stylesheet retry stays testable without a DOM. */
    reloadStylesheet?: (url: string, nonce: number) => Promise<boolean>;
  } = {},
): () => Promise<P> {
  const importer = options.importer ?? ((url: string) => import(/* @vite-ignore */ url));
  const reloadStylesheet = options.reloadStylesheet ?? reloadPageStylesheet;
  return () => {
    if (!failedPages.has(key)) {
      return direct().catch((error: unknown) => {
        failedPages.add(key);
        const cssUrl = cssUrlFromError(error);
        if (cssUrl) failedPageCss.set(key, cssUrl);
        throw error;
      });
    }
    retryNonce += 1;
    const nonce = retryNonce;
    const requestModule = () => {
      const url = lazyPageRetryUrl(key, document.baseURI, nonce, options.urls);
      if (!url) return direct();
      return importer(url).then(
        (module) => ((module as { default?: P }).default ?? (module as P)),
      );
    };
    const cssUrl = failedPageCss.get(key);
    if (!cssUrl) return requestModule();
    // The reported stylesheet failed first, so recover it before the module;
    // a still-failing stylesheet must not be reported as a successful retry.
    return reloadStylesheet(cssUrl, nonce).then((loaded) => {
      if (!loaded) throw new Error(`Unable to preload CSS for ${cssUrl}`);
      return requestModule();
    });
  };
}
