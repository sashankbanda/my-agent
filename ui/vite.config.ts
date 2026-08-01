import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, Vite serves the UI and proxies API/WS calls to the kernel.
// In production the kernel serves ui/dist itself, so everything is same-origin.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
      "/chat": "http://127.0.0.1:8765",
      "/sessions": "http://127.0.0.1:8765",
      "/health": "http://127.0.0.1:8765",
    },
  },
});
