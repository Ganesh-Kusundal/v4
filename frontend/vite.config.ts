import { defineConfig } from "vite";

// Dev mode proxies API and WebSocket traffic to the FastAPI backend, which is
// the only brain: every fetch and socket message in dev lands on the same
// code paths production uses. Production serves dist/ from the backend at
// /ui (single origin), so the proxy exists only for `npm run dev`.
export default defineConfig({
  // Relative asset URLs: production serves dist/ under /ui (not domain root),
  // so "/assets/..." would 404. "./" makes index.html resolve its bundle
  // against the page URL, which is correct both at /ui/ and at root.
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
