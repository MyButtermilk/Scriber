import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "client", "src"),
    },
  },
  test: {
    name: "components",
    environment: "jsdom",
    execArgv: ["--no-experimental-webstorage"],
    setupFiles: [path.resolve(import.meta.dirname, "vitest.setup.ts")],
    include: ["client/src/**/*.test.tsx"],
    clearMocks: true,
    restoreMocks: true,
  },
});
