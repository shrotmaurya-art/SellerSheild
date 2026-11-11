/// <reference types="vitest" />
import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  envDir: path.resolve(__dirname, ".."),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/demo": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/sim": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/mail": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/agent": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/agent/, ""),
      },
    },
  },
});

