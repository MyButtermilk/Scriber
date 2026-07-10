import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import { readFileSync } from "node:fs";
import { metaImagesPlugin } from "./vite-plugin-meta-images";

const packageJson = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "package.json"), "utf-8"));

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    metaImagesPlugin(),
    ...(process.env.NODE_ENV !== "production" &&
      process.env.REPL_ID !== undefined
      ? [
        await import("@replit/vite-plugin-cartographer").then((m) =>
          m.cartographer(),
        ),
        await import("@replit/vite-plugin-dev-banner").then((m) =>
          m.devBanner(),
        ),
      ]
      : []),
  ],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "client", "src"),
      "@shared": path.resolve(import.meta.dirname, "shared"),
      "@assets": path.resolve(import.meta.dirname, "attached_assets"),
    },
  },
  define: {
    __SCRIBER_APP_VERSION__: JSON.stringify(packageJson.version || ""),
  },
  css: {
    postcss: {
      plugins: [],
    },
  },
  root: path.resolve(import.meta.dirname, "client"),
  build: {
    outDir: path.resolve(import.meta.dirname, "dist/public"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalizedId = id.replace(/\\/g, "/");
          if (!normalizedId.includes("/node_modules/")) {
            return undefined;
          }
          if (
            normalizedId.includes("/node_modules/react/") ||
            normalizedId.includes("/node_modules/react-dom/") ||
            normalizedId.includes("/node_modules/scheduler/")
          ) {
            return "vendor-react";
          }
          if (normalizedId.includes("/node_modules/@tanstack/")) {
            return "vendor-query";
          }
          if (
            normalizedId.includes("/node_modules/framer-motion/") ||
            normalizedId.includes("/node_modules/motion/") ||
            normalizedId.includes("/node_modules/motion-dom/") ||
            normalizedId.includes("/node_modules/motion-utils/")
          ) {
            return "vendor-motion";
          }
          if (
            normalizedId.includes("/node_modules/recharts/") ||
            normalizedId.includes("/node_modules/d3-")
          ) {
            return "vendor-charts";
          }
          if (
            normalizedId.includes("/node_modules/react-markdown/") ||
            normalizedId.includes("/node_modules/remark-") ||
            normalizedId.includes("/node_modules/mdast-") ||
            normalizedId.includes("/node_modules/micromark") ||
            normalizedId.includes("/node_modules/unist-") ||
            normalizedId.includes("/node_modules/hast-") ||
            normalizedId.includes("/node_modules/vfile") ||
            normalizedId.includes("/node_modules/property-information/") ||
            normalizedId.includes("/node_modules/comma-separated-tokens/") ||
            normalizedId.includes("/node_modules/space-separated-tokens/") ||
            normalizedId.includes("/node_modules/decode-named-character-reference/") ||
            normalizedId.includes("/node_modules/trim-lines/") ||
            normalizedId.includes("/node_modules/trough/") ||
            normalizedId.includes("/node_modules/zwitch/")
          ) {
            return "vendor-markdown";
          }
          if (normalizedId.includes("/node_modules/lucide-react/")) {
            return "vendor-icons";
          }
          if (
            normalizedId.includes("/node_modules/@radix-ui/") ||
            normalizedId.includes("/node_modules/@floating-ui/") ||
            normalizedId.includes("/node_modules/react-remove-scroll") ||
            normalizedId.includes("/node_modules/react-style-singleton/") ||
            normalizedId.includes("/node_modules/use-callback-ref/") ||
            normalizedId.includes("/node_modules/use-sidecar/") ||
            normalizedId.includes("/node_modules/aria-hidden/") ||
            normalizedId.includes("/node_modules/cmdk/")
          ) {
            return "vendor-ui";
          }
          if (normalizedId.includes("/node_modules/@tauri-apps/")) {
            return "vendor-tauri";
          }
          if (
            normalizedId.includes("/node_modules/react-dropzone/") ||
            normalizedId.includes("/node_modules/file-selector/") ||
            normalizedId.includes("/node_modules/attr-accept/")
          ) {
            return "vendor-upload";
          }
          return "vendor";
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: true,
    fs: {
      strict: true,
      deny: ["**/.*"],
    },
  },
});
