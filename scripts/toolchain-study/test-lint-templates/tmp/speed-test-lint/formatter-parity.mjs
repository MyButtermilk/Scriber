import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";

const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const require = createRequire(path.join(frontend, "package.json"));
const prettier = require("prettier");
const toolRoot = process.env.SCRIBER_LINT_TOOLS ?? path.resolve(frontend, "../../lint-tools");
const { format } = await import(pathToFileURL(path.join(toolRoot, "node_modules/oxfmt/dist/index.js")).href);
const sourceRevision = process.env.SCRIBER_SOURCE_REVISION ?? "d061ad75a0b39ca1b299eb8174ac828b122ef136";
const original = spawnSync("git", ["ls-tree", "--full-tree", "-r", "--name-only", sourceRevision, "Frontend"], { cwd: root, encoding: "utf8" });
if (original.error || original.signal || original.status !== 0) throw new Error("Cannot enumerate the frozen formatter baseline");
const originalFiles = original.stdout.trim().split("\n").filter(Boolean);
if (originalFiles.length !== 353 || originalFiles.some((name) => !name.startsWith("Frontend/"))) throw new Error("The frozen formatter baseline must contain all 353 original tracked Frontend paths");
const config = JSON.parse(readFileSync(path.join(frontend, ".prettierrc.json"), "utf8"));
const oxfmtConfig = { ...config, sortPackageJson: false };
delete oxfmtConfig.endOfLine;
const results = [];
const output = path.join(import.meta.dirname, "formatter-results");
mkdirSync(output, { recursive: true });
for (const name of originalFiles) {
  const relative = name.replace(/^Frontend\//, "");
  const absolute = path.join(frontend, relative);
  const info = await prettier.getFileInfo(absolute, { ignorePath: path.join(frontend, ".prettierignore") });
  if (info.ignored || !info.inferredParser) continue;
  const source = readFileSync(absolute, "utf8");
  const expected = await prettier.format(source, { ...config, filepath: absolute });
  let actual;
  try {
    actual = await format(relative, source, oxfmtConfig);
  } catch (error) {
    actual = { code: "", errors: [{ message: error.message }] };
  }
  const entry = { file: relative, parser: info.inferredParser, originalPrettierClean: source === expected, oxfmtMatchesPrettier: expected === actual.code && !actual.errors?.length, errors: actual.errors };
  results.push(entry);
  if (!entry.oxfmtMatchesPrettier) {
    const safeName = relative.replaceAll("/", "__");
    writeFileSync(path.join(output, `${safeName}.prettier`), expected);
    writeFileSync(path.join(output, `${safeName}.oxfmt`), actual.code ?? "");
  }
}
const inventorySha256 = createHash("sha256").update(JSON.stringify(results.map((entry) => entry.file).sort())).digest("hex");
if (results.length !== 261 || inventorySha256 !== "82fd072a0b6f04376be0aecc082f4ff465ece049bbcd4a38bd706d33f8073b93") throw new Error(`The formatter comparison omitted or changed the original 261-file scope: ${results.length} files, ${inventorySha256}`);
const eolInput = "export const example = 1;\r\nexport const second = 2;\r\n";
const prettierEol = await prettier.format(eolInput, { ...config, filepath: "eol-probe.ts" });
const oxfmtEol = await format("eol-probe.ts", eolInput, oxfmtConfig);
let autoOptionError;
try {
  const autoResult = await format("eol-probe.ts", eolInput, { ...oxfmtConfig, endOfLine: "auto" });
  if (autoResult.errors?.length) autoOptionError = autoResult.errors.map((error) => error.message).join("; ");
} catch (error) { autoOptionError = error.message; }
const bunFmt = spawnSync(process.env.SCRIBER_BUN_EXE ?? "bun", ["fmt", "--help"], { cwd: frontend, encoding: "utf8" });
const report = {
  sourceRevision, scope: "Original tracked Frontend files with the unchanged Prettier ignore rules; experimental configs are excluded",
  examinedFiles: results.length, inventorySha256, mismatches: results.filter((entry) => !entry.oxfmtMatchesPrettier),
  originalPrettierDifferences: results.filter((entry) => !entry.originalPrettierClean),
  eol: { prettierPreservesCrLf: prettierEol.includes("\r\n"), oxfmtPreservesCrLf: oxfmtEol.code.includes("\r\n"), unsupportedAutoOption: autoOptionError ?? null },
  bunFormatter: { exitCode: bunFmt.status, error: bunFmt.error?.message ?? null, output: `${bunFmt.stdout ?? ""}${bunFmt.stderr ?? ""}` },
  files: results,
};
writeFileSync(path.join(import.meta.dirname, "formatter-parity-results.json"), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify({ ...report, files: undefined }, null, 2));
