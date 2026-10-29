import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the API to a backend on :8000 (same origin as in Compose,
// where nginx does the proxying).
export default defineConfig({
  plugins: [react()],
  server: { port: 3000, proxy: { "/api": "http://localhost:8000" } },
});
