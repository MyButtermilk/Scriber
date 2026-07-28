import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import { readFileSync } from "node:fs";

const packageJson = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "package.json"), "utf-8"));

function parseExplicitAllowedHosts(value: string | undefined): string[] {
  const hosts = (value ?? "")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean);

  for (const host of hosts) {
    if (
      host === "*" ||
      host === "true" ||
      host.startsWith(".") ||
      host.includes("://") ||
      host.includes("/") ||
      /\s/.test(host)
    ) {
      throw new Error(`Invalid SCRIBER_VITE_ALLOWED_HOSTS entry "${host}". Use exact hostnames separated by commas.`);
    }
  }

  return [...new Set(hosts)];
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, import.meta.dirname, "");
  const allowLan = env.SCRIBER_VITE_ALLOW_LAN === "1";
  const explicitAllowedHosts = parseExplicitAllowedHosts(env.SCRIBER_VITE_ALLOWED_HOSTS);

  if (allowLan && explicitAllowedHosts.length === 0) {
    throw new Error("SCRIBER_VITE_ALLOW_LAN=1 requires explicit SCRIBER_VITE_ALLOWED_HOSTS.");
  }

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(import.meta.dirname, "client", "src"),
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
              normalizedId.includes("/node_modules/motion/") ||
              normalizedId.includes("/node_modules/motion-dom/") ||
              normalizedId.includes("/node_modules/motion-utils/")
            ) {
              return "vendor-motion";
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
      host: allowLan ? "0.0.0.0" : "127.0.0.1",
      allowedHosts: ["localhost", ...(allowLan ? explicitAllowedHosts : [])],
      fs: {
        strict: true,
        deny: ["**/.*"],
      },
    },
  };
});
