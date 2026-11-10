import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Same-origin proxies (spec F-1): no CORS on the backend, and the token never crosses
// origins. `/api` is Part A; `/smarthealth.assistant.v1.Assistant/` is gRPC-Web to the
// assistant, through Envoy (spec D-30). nginx does the same in the compose image.
const envoy = process.env.SMARTHEALTH_ENVOY ?? "http://localhost:8088";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SMARTHEALTH_API ?? "http://localhost:8000",
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/smarthealth.assistant.v1.Assistant": { target: envoy },
    },
  },
  test: { environment: "jsdom" },
});
