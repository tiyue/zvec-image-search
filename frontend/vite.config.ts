/// <reference types="vitest/config" />

import path from "node:path";
import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig, type Plugin } from "vite";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

/**
 * Lazy feature pages and the source-relative path of their chunk. The build
 * rewrites the placeholder in `src/composables/useLazyPage.ts` with the emitted
 * file name of each page chunk, so a manual retry of a page whose first import
 * failed can request the module at a fresh URL. Nothing here hardcodes a hash
 * or touches the asset base.
 */
const LAZY_PAGE_CHUNKS: Record<string, string> = {
  tasks: "src/features/tasks/TasksPage.vue",
  batch: "src/features/organize/OrganizePage.vue",
  recommendations: "src/features/recommendations/RecommendationPage.vue",
  "raw-selection": "src/features/raw-selection/RawSelectionPage.vue",
  "image-edit": "src/features/image-edit/ImageEditPage.vue",
  settings: "src/features/settings/SettingsPage.vue",
};

const LAZY_PAGE_CHUNK_MARKER = "__zvec_lazy_page_chunks__";

function lazyPageChunkUrls(): Plugin {
  return {
    name: "yaolens-lazy-page-chunk-urls",
    renderChunk(code, _chunk, _outputOptions, meta) {
      if (!code.includes(LAZY_PAGE_CHUNK_MARKER)) return null;
      const urls: Record<string, string> = {};
      for (const chunk of Object.values(meta.chunks)) {
        const facade = chunk.facadeModuleId;
        if (!facade) continue;
        const relative = path.relative(projectRoot, facade).split(path.sep).join("/");
        for (const [page, modulePath] of Object.entries(LAZY_PAGE_CHUNKS)) {
          if (relative === modulePath) urls[page] = chunk.fileName;
        }
      }
      return code.split(JSON.stringify(LAZY_PAGE_CHUNK_MARKER)).join(JSON.stringify(urls));
    },
  };
}

export default defineConfig({
  // Relative asset URLs keep the frozen pywebview build independent of its port.
  base: "./",
  plugins: [
    vue({
      features: {
        // Keep production scope IDs stable across LF and CRLF checkouts.
        componentIdGenerator: "filepath",
      },
    }),
    lazyPageChunkUrls(),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../zvec_webview/frontend_dist",
    emptyOutDir: true,
    target: "es2022",
    manifest: true,
    sourcemap: false,
    // Async feature pages share the app-level components/composables/api
    // modules with the entry (search-learning is shared with the settings
    // page). Without an explicit group those shared modules stay in the entry
    // chunk, so every page chunk imports the entry while the entry dynamically
    // imports the pages — an import cycle the WebView manifest validator
    // rejects. Hoisting only those shared modules into their own chunk breaks
    // the cycle and keeps every feature page lazy.
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "shared",
              test:
                /[\\/]src[\\/](components|composables|api|types)[\\/]|[\\/]src[\\/]features[\\/]search-learning[\\/]/u,
            },
          ],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.ts"],
    clearMocks: true,
    restoreMocks: true,
    css: true,
  },
});
