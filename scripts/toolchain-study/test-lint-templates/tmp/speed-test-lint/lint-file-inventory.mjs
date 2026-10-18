import { globSync } from "node:fs";

// ESLint always discovers js/mjs/cjs; the frozen config adds ts/tsx. Keep its
// five global excludes and its default nested node_modules/.git excludes.
// The scope parity probe compares this list with both CLIs' actual file lists.
export function lintFiles(frontend) {
  const files = globSync("**/*.{js,mjs,cjs,ts,tsx}", {
    cwd: frontend,
    exclude: [
      "dist/**", "node_modules/**", "output/**", "src-tauri/**", "client/public/**",
      "**/node_modules/**", "**/.git/**",
    ],
  }).map((file) => `./${file.replaceAll("\\", "/")}`).sort();
  if (files.length === 0) throw new Error("The lint candidate must inspect a nonempty source inventory");
  return files;
}

export function oxlintArgs(frontend, config, extraArgs = []) {
  // The isolated study is beneath the repository's gitignored tmp/. Oxlint
  // skips that whole tree when given '.', even with --no-ignore. Explicit file
  // paths preserve the original ESLint scope without weakening config ignores.
  // Agent-aware automatic output can omit the successful file-count summary.
  // Select the same normal format on every runner so each timed sample can
  // prove its file count, while preserving an explicitly requested reporter.
  const explicitFormat = extraArgs.some((argument) => argument === "--format" || argument.startsWith("--format=") || argument === "-f" || argument.startsWith("-f="));
  return ["--config", config, "--max-warnings", "0", ...(explicitFormat ? [] : ["--format", "default"]), ...extraArgs, ...lintFiles(frontend)];
}
