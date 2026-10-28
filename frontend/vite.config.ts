import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Same-origin /api proxy (spec F-1): no CORS on the backend, and the token never
// crosses origins. nginx does the same in the compose image.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SMARTHEALTH_API ?? "http://localhost:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: { environment: "jsdom" },
});
