import { availableParallelism } from "node:os";
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
    // Workers persist and load jsdom once instead of once per test file. With at
    // least two workers, Vitest schedules every file separately, so each file
    // still gets a fresh VM context, jsdom window, storage and module graph.
    // vitest.setup.ts fails the run if files ever share one context.
    pool: "vmThreads",
    // Four workers suit the four-vCPU CI runners. Smaller hosts are not
    // oversubscribed, but never drop below the two workers isolation requires.
    maxWorkers: Math.max(2, Math.min(4, availableParallelism())),
    execArgv: ["--no-experimental-webstorage"],
    setupFiles: [path.resolve(import.meta.dirname, "vitest.setup.ts")],
    include: ["client/src/**/*.test.tsx"],
    clearMocks: true,
    restoreMocks: true,
  },
});
