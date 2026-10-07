import { defineComponent } from "vue";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createLazyPageLoader,
  cssUrlFromError,
  lazyPageRetryUrl,
  reloadPageStylesheet,
} from "../useLazyPage";

const page = defineComponent({ name: "LoadedPage", render: () => null });

describe("lazyPageRetryUrl", () => {
  it("builds a chunk URL only for a known page", () => {
    const urls = { settings: "assets/SettingsPage-abc123.js" };

    expect(lazyPageRetryUrl("settings", "http://localhost/index.html", 2, urls)).toBe(
      "http://localhost/assets/SettingsPage-abc123.js?zvecRetry=2",
    );
    expect(lazyPageRetryUrl("tasks", "http://localhost/", 2, urls)).toBeNull();
  });

  it("changes the URL for every retry so the failed module cannot be reused", () => {
    const urls = { settings: "assets/SettingsPage-abc123.js" };

    const first = lazyPageRetryUrl("settings", "http://localhost/", 1, urls);
    const second = lazyPageRetryUrl("settings", "http://localhost/", 2, urls);
    expect(second).not.toBe(first);
    expect(second).toContain("zvecRetry=2");
  });
});

describe("cssUrlFromError", () => {
  it("reads the URL Vite reported for a failed CSS preload", () => {
    expect(
      cssUrlFromError(
        new Error(
          "Unable to preload CSS for http://127.0.0.1:49374/assets/SettingsPage-BosJCI-G.css",
        ),
      ),
    ).toBe("http://127.0.0.1:49374/assets/SettingsPage-BosJCI-G.css");
  });

  it("returns an empty string for unrelated failures", () => {
    expect(cssUrlFromError(new Error("failed to fetch dynamically imported module"))).toBe("");
    expect(cssUrlFromError(undefined)).toBe("");
    expect(cssUrlFromError("plain string")).toBe("");
  });
});

describe("reloadPageStylesheet", () => {
  const cssUrl = "http://localhost/assets/SettingsPage-abc.css";

  function appendStylesheetLink(href: string): HTMLLinkElement {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    document.head.appendChild(link);
    return link;
  }

  afterEach(() => {
    document.head
      .querySelectorAll('link[rel="stylesheet"]')
      .forEach((node) => node.remove());
  });

  it("re-requests the existing link at a fresh query and resolves on load", async () => {
    const link = appendStylesheetLink(cssUrl);
    const addSpy = vi.spyOn(link, "addEventListener");
    const removeSpy = vi.spyOn(link, "removeEventListener");

    const pending = reloadPageStylesheet(cssUrl, 3);
    expect(link.href).toBe(`${cssUrl}?zvecCssRetry=3`);
    link.dispatchEvent(new Event("load"));
    await expect(pending).resolves.toBe(true);

    // Both listeners must be released with the very handlers that were added,
    // so a later manual retry cannot accumulate lingering closures.
    expect(addSpy).toHaveBeenCalledWith("load", expect.any(Function));
    expect(addSpy).toHaveBeenCalledWith("error", expect.any(Function));
    const loadHandler = addSpy.mock.calls.find(([type]) => type === "load")?.[1];
    const errorHandler = addSpy.mock.calls.find(([type]) => type === "error")?.[1];
    expect(removeSpy).toHaveBeenCalledWith("load", loadHandler);
    expect(removeSpy).toHaveBeenCalledWith("error", errorHandler);
  });

  it("resolves false when the re-request errors so the retry is not a success", async () => {
    const link = appendStylesheetLink(cssUrl);
    const addSpy = vi.spyOn(link, "addEventListener");
    const removeSpy = vi.spyOn(link, "removeEventListener");

    const pending = reloadPageStylesheet(cssUrl, 4);
    expect(link.href).toContain("zvecCssRetry=4");
    link.dispatchEvent(new Event("error"));
    await expect(pending).resolves.toBe(false);

    expect(addSpy).toHaveBeenCalledWith("load", expect.any(Function));
    expect(addSpy).toHaveBeenCalledWith("error", expect.any(Function));
    const loadHandler = addSpy.mock.calls.find(([type]) => type === "load")?.[1];
    const errorHandler = addSpy.mock.calls.find(([type]) => type === "error")?.[1];
    expect(removeSpy).toHaveBeenCalledWith("load", loadHandler);
    expect(removeSpy).toHaveBeenCalledWith("error", errorHandler);
  });

  it("finds the link again after a previous retry rewrote its href", async () => {
    const link = appendStylesheetLink(cssUrl);
    // The href left behind by an earlier failed retry.
    link.href = `${cssUrl}?zvecCssRetry=1`;

    const pending = reloadPageStylesheet(cssUrl, 5);
    expect(link.href).toBe(`${cssUrl}?zvecCssRetry=5`);
    link.dispatchEvent(new Event("load"));
    await expect(pending).resolves.toBe(true);
  });

  it("proceeds when no matching stylesheet link exists", async () => {
    await expect(
      reloadPageStylesheet("http://localhost/assets/missing.css", 6),
    ).resolves.toBe(true);
  });
});

describe("createLazyPageLoader", () => {
  it("uses the direct import first and never retries on its own", async () => {
    const direct = vi.fn(async (): Promise<typeof page> => {
      throw new Error("failed to fetch dynamically imported module");
    });
    const importer = vi.fn(async (_url: string): Promise<unknown> => ({ default: page }));
    const load = createLazyPageLoader("direct-first", direct, {
      urls: { "direct-first": "assets/Direct-abc.js" },
      importer,
    });

    await expect(load()).rejects.toThrow("failed to fetch dynamically imported module");
    expect(direct).toHaveBeenCalledTimes(1);
    expect(importer).not.toHaveBeenCalled();
  });

  it("re-requests the emitted chunk at a fresh URL after a failure", async () => {
    const direct = vi.fn(async (): Promise<typeof page> => {
      throw new Error("failed to fetch dynamically imported module");
    });
    const importer = vi.fn(async (_url: string): Promise<unknown> => ({ default: page }));
    const load = createLazyPageLoader("retry-chunk", direct, {
      urls: { "retry-chunk": "assets/Retry-abc.js" },
      importer,
    });

    await expect(load()).rejects.toThrow("failed to fetch dynamically imported module");
    await expect(load()).resolves.toBe(page);
    await expect(load()).resolves.toBe(page);

    // The plain import is attempted once; each later attempt is a real request.
    expect(direct).toHaveBeenCalledTimes(1);
    expect(importer).toHaveBeenCalledTimes(2);
    expect(importer.mock.calls[0][0]).toContain("assets/Retry-abc.js?zvecRetry=");
    expect(importer.mock.calls[1][0]).not.toBe(importer.mock.calls[0][0]);
  });

  it("falls back to the direct import when no chunk URL is known", async () => {
    let attempts = 0;
    const direct = vi.fn(async (): Promise<typeof page> => {
      attempts += 1;
      if (attempts === 1) throw new Error("module unavailable");
      return page;
    });
    const importer = vi.fn(async (_url: string): Promise<unknown> => ({ default: page }));
    const load = createLazyPageLoader("no-chunk-url", direct, { urls: {}, importer });

    await expect(load()).rejects.toThrow("module unavailable");
    await expect(load()).resolves.toBe(page);
    expect(direct).toHaveBeenCalledTimes(2);
    expect(importer).not.toHaveBeenCalled();
  });
});

describe("createLazyPageLoader stylesheet recovery", () => {
  it("re-requests the reported stylesheet before importing the retried chunk", async () => {
    const cssUrl = "http://localhost/assets/SettingsPage-abc.css";
    const direct = vi.fn(async (): Promise<typeof page> => {
      throw new Error(`Unable to preload CSS for ${cssUrl}`);
    });
    const importer = vi.fn(async (_url: string): Promise<unknown> => ({ default: page }));
    const reloadStylesheet = vi.fn(async (_url: string, _nonce: number) => true);
    const load = createLazyPageLoader("css-success", direct, {
      urls: { "css-success": "assets/SettingsPage-abc.js" },
      importer,
      reloadStylesheet,
    });

    await expect(load()).rejects.toThrow("Unable to preload CSS");
    expect(reloadStylesheet).not.toHaveBeenCalled();

    await expect(load()).resolves.toBe(page);
    expect(reloadStylesheet).toHaveBeenCalledTimes(1);
    expect(reloadStylesheet.mock.calls[0][0]).toBe(cssUrl);
    expect(typeof reloadStylesheet.mock.calls[0][1]).toBe("number");
    expect(importer).toHaveBeenCalledTimes(1);
    expect(reloadStylesheet.mock.invocationCallOrder[0]).toBeLessThan(
      importer.mock.invocationCallOrder[0],
    );
  });

  it("does not treat a still-failing stylesheet as a successful retry", async () => {
    const cssUrl = "http://localhost/assets/Broken-abc.css";
    const direct = vi.fn(async (): Promise<typeof page> => {
      throw new Error(`Unable to preload CSS for ${cssUrl}`);
    });
    const importer = vi.fn(async (_url: string): Promise<unknown> => ({ default: page }));
    const reloadStylesheet = vi.fn(async (_url: string, _nonce: number) => false);
    const load = createLazyPageLoader("css-failure", direct, {
      urls: { "css-failure": "assets/Broken-abc.js" },
      importer,
      reloadStylesheet,
    });

    // The retry stays reachable and never falls back to a module request that
    // would render an unstyled page.
    await expect(load()).rejects.toThrow("Unable to preload CSS");
    expect(reloadStylesheet).not.toHaveBeenCalled();
    await expect(load()).rejects.toThrow("Unable to preload CSS");
    await expect(load()).rejects.toThrow("Unable to preload CSS");
    expect(reloadStylesheet).toHaveBeenCalledTimes(2);
    expect(importer).not.toHaveBeenCalled();
  });
});
