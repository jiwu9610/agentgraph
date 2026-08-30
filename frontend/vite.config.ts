/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite config is infrastructure — it's filled in for you. Two things to notice:
//
// 1. The React plugin gives us JSX + Fast Refresh in dev.
// 2. The `test` block configures Vitest. We use the jsdom environment so that
//    `document`, `window`, and `localStorage` exist in Node when tests run —
//    React components need a DOM to render into. `setupTests.ts` wires up the
//    `@testing-library/jest-dom` matchers (e.g. `.toBeInTheDocument()`).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/setupTests.ts",
    css: false,
  },
});
