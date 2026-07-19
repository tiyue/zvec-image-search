/// <reference types="vitest/config" />

import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  // Relative asset URLs keep the frozen pywebview build independent of its port.
  base: "./",
  plugins: [vue()],
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
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.{test,spec}.ts"],
    clearMocks: true,
    restoreMocks: true,
    css: true,
  },
});
