import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { lintFiles, oxlintArgs } from "./lint-file-inventory.mjs";

const root = path.resolve(import.meta.dirname, "../..");
const frontend = path.join(root, "Frontend");
const toolRoot = process.env.SCRIBER_LINT_TOOLS ?? path.resolve(frontend, "../../lint-tools");
const eslint = path.join(frontend, "node_modules/eslint/bin/eslint.js");
const oxlint = path.join(toolRoot, "node_modules/oxlint/bin/oxlint");
const harness = path.join(import.meta.dirname, "run-candidate.mjs");
const logs = path.join(import.meta.dirname, "lint-scope-probes");
const records = [];
mkdirSync(logs, { recursive: true });
const normalize = (file) => (path.isAbsolute(file) ? path.relative(frontend, file) : file).replaceAll("\\", "/").replace(/^\.\//, "");
const same = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const successful = (result, code = 0) => !result.error && !result.signal && result.status === code;
const run = (label, args) => {
  const result = spawnSync(process.execPath, args, { cwd: frontend, env: process.env, encoding: "utf8", timeout: 60000, maxBuffer: 16 * 1024 * 1024 });
  writeFileSync(path.join(logs, `${label}.log`), `${result.stdout ?? ""}${result.stderr ?? ""}`);
  return result;
};
const commandEvidence = (result) => ({ exitCode: result.status, signal: result.signal, error: result.error?.message ?? null });
const eslintFiles = (result) => JSON.parse(result.stdout).map((entry) => normalize(entry.filePath)).sort();
const oxlintFiles = (result) => result.stdout.trim().split(/\r?\n/).filter(Boolean).map(normalize).sort();
const nativeInventory = (label) => run(label, [oxlint, ...oxlintArgs(frontend, ".oxlintrc.residual.json", ["--debug", "files"])]);
const ignoredDirectories = ["dist", "node_modules", "output", "src-tauri", "client/public", "client/src/__speed_scope_nested/node_modules"];
const fixtures = [
  { file: "client/src/lib/__speed_lint_scope_probe.ts", source: "debugger;\nexport {};\n", included: true },
  { file: "client/src/lib/__speed_lint_scope_probe.mjs", source: "export {};\n", included: true },
  { file: "client/src/lib/__speed_lint_scope_probe.cjs", source: "module.exports = {};\n", included: true },
  ...ignoredDirectories.map((directory) => ({ file: `${directory}/__speed_lint_scope_probe/invalid.ts`, source: "const = deliberately invalid;\n", included: false })),
];
const created = [];
let originalFiles;
try {
  const revision = process.env.SCRIBER_SOURCE_REVISION ?? "d061ad75a0b39ca1b299eb8174ac828b122ef136";
  const git = spawnSync("git", ["ls-tree", "--full-tree", "-r", "--name-only", revision, "Frontend"], { cwd: root, encoding: "utf8" });
  if (!successful(git)) throw new Error("Unable to enumerate the frozen tracked frontend source");
  const tracked = git.stdout.trim().split(/\r?\n/).map((file) => file.replace(/^Frontend\//, ""));
  const sourceFiles = tracked.filter((file) => /\.(?:js|mjs|cjs|ts|tsx)$/.test(file) && !/^(?:dist|node_modules|output|src-tauri|client\/public)\//.test(file)).sort();
  if (tracked.length !== 353 || sourceFiles.length === 0) throw new Error("Unexpected frozen tracked frontend inventory");
  originalFiles = [...sourceFiles, "eslint.residual.config.js"].sort();
  const baseline = run("complete-eslint", [eslint, ".", "--max-warnings", "0", "--format", "json"]);
  const native = nativeInventory("complete-oxlint-files");
  const baselineFiles = eslintFiles(baseline);
  const nativeFiles = oxlintFiles(native);
  records.push({
    id: "complete-file-inventory", sourceRevision: revision,
    trackedSourceFiles: sourceFiles.length, files: originalFiles.length,
    expectedFiles: originalFiles, eslintFiles: baselineFiles, oxlintFiles: nativeFiles,
    inventorySha256: createHash("sha256").update(JSON.stringify(originalFiles)).digest("hex"),
    eslint: commandEvidence(baseline), oxlint: commandEvidence(native),
    pass: successful(baseline) && successful(native) && same(baselineFiles, originalFiles) && same(nativeFiles, originalFiles) && same(lintFiles(frontend).map(normalize), originalFiles),
  });

  // Preserve proof of the original harness error; --no-ignore would not repair
  // gitignore filtering during directory traversal either.
  const oldInvocation = run("gitignored-directory-negative-control", [oxlint, ".", "--config", ".oxlintrc.residual.json", "--max-warnings", "0"]);
  records.push({
    id: "directory-ignore-negative-control", ...commandEvidence(oldInvocation),
    pass: successful(oldInvocation, 1) && /No files found to lint/.test(`${oldInvocation.stdout}${oldInvocation.stderr}`),
  });

  for (const fixture of fixtures) {
    const file = path.join(frontend, fixture.file);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, fixture.source, { flag: "wx" });
    created.push(file);
  }
  const expectedWithFixtures = [...originalFiles, ...fixtures.filter((fixture) => fixture.included).map((fixture) => fixture.file)].sort();
  const changedBaseline = run("source-and-ignore-eslint", [eslint, ".", "--max-warnings", "0", "--format", "json"]);
  const changedNative = nativeInventory("source-and-ignore-oxlint-files");
  const changedHybrid = run("source-and-ignore-hybrid", [harness, "lint-oxlint-hybrid"]);
  const baselineDiagnostics = JSON.parse(changedBaseline.stdout).flatMap((file) => file.messages.map((message) => ({ file: normalize(file.filePath), ruleId: message.ruleId })));
  const ignored = fixtures.filter((fixture) => !fixture.included).map((fixture) => fixture.file);
  records.push({
    id: "source-discovery-and-ignore-controls", files: expectedWithFixtures.length,
    included: fixtures.filter((fixture) => fixture.included).map((fixture) => fixture.file), ignored,
    eslint: commandEvidence(changedBaseline), oxlint: commandEvidence(changedNative), hybrid: commandEvidence(changedHybrid),
    diagnostics: baselineDiagnostics,
    pass: successful(changedBaseline, 1) && successful(changedNative) && successful(changedHybrid, 1)
      && same(eslintFiles(changedBaseline), expectedWithFixtures) && same(oxlintFiles(changedNative), expectedWithFixtures)
      && same(lintFiles(frontend).map(normalize), expectedWithFixtures)
      && baselineDiagnostics.length === 1 && baselineDiagnostics[0].file === fixtures[0].file && baselineDiagnostics[0].ruleId === "no-debugger"
      && /no-debugger/.test(`${changedHybrid.stdout}${changedHybrid.stderr}`),
  });
} catch (error) {
  records.push({ id: "scope-probe-error", error: error.stack ?? error.message, pass: false });
} finally {
  for (const file of created) rmSync(file, { force: true });
}

try {
  const restored = run("restored-clean-hybrid", [harness, "lint-oxlint-hybrid"]);
  records.push({ id: "restored-clean-hybrid", ...commandEvidence(restored), pass: successful(restored) && Array.isArray(originalFiles) && same(lintFiles(frontend).map(normalize), originalFiles) });
} catch (error) {
  records.push({ id: "restored-clean-hybrid", error: error.stack ?? error.message, pass: false });
}
writeFileSync(path.join(import.meta.dirname, "lint-scope-parity-results.json"), JSON.stringify(records, null, 2) + "\n");
console.log(JSON.stringify({ cases: records.length, passed: records.filter((record) => record.pass).length, records: records.map(({ expectedFiles, eslintFiles, oxlintFiles, ...record }) => record) }, null, 2));
if (records.length !== 4 || records.some((record) => !record.pass)) process.exitCode = 1;
