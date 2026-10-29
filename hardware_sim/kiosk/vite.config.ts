import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the machine API to a simulator on :8080.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8080" } },
});
