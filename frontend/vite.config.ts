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
    // Keep the browser's Host header: the server only accepts writes whose Origin
    // matches it (the string shorthand would set changeOrigin and get every write a 403).
    proxy: {
      "/api": { target: api, changeOrigin: false },
      "/ws": { target: api.replace(/^http/, "ws"), ws: true, changeOrigin: false },
    },
  },
  test: {
    environment: "node",
  },
});
