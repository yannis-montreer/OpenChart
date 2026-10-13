/// <reference types="vitest/config" />
import { defineConfig } from "vite";

// The Python server serves the build from src/openchart/web. During development
// `npm run dev` proxies the API and websocket to a server on OPENCHART_API (default :8000).
const api = process.env.OPENCHART_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  build: {
    outDir: "../src/openchart/web",
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": api,
      "/ws": { target: api.replace(/^http/, "ws"), ws: true },
    },
  },
  test: {
    environment: "node",
  },
});
