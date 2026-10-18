/**
 * Untimed comparison of the full Oxlint migration, a React-only ESLint
 * fallback, and the complete hybrid against the original 29 ESLint controls.
 * Usage: node verify-lint-migrations.mjs --frontend ABS --lint-tools ABS --output ABS
 * No Bun/runtime-tools dependency and no timing claims.
 */
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { parseArgs } from "node:util";
import { pathToFileURL, fileURLToPath } from "node:url";

const { values } = parseArgs({ options: Object.fromEntries(["frontend", "lint-tools", "output"].map((name) => [name, { type: "string" }])) });
for (const name of ["frontend", "lint-tools", "output"]) {
  if (!values[name] || !path.isAbsolute(values[name])) throw new Error("--" + name + " must be absolute");
}
const frontend = fs.realpathSync(values.frontend);
const toolRoot = fs.realpathSync(values["lint-tools"]);
const output = path.resolve(values.output);
const relativeOutput = path.relative(frontend, output);
if (!relativeOutput || (!relativeOutput.startsWith(".." + path.sep) && relativeOutput !== ".." && !path.isAbsolute(relativeOutput))) throw new Error("Output must be outside Frontend");
if (fs.existsSync(output) && fs.readdirSync(output).length) throw new Error("Use an empty output directory");
fs.mkdirSync(output, { recursive: true });
const probeRoot = path.resolve(frontend, "../tmp/speed-test-lint");
const { lintFiles, oxlintArgs } = await import(pathToFileURL(path.join(probeRoot, "lint-file-inventory.mjs")).href);
const eslint = path.join(frontend, "node_modules/eslint/bin/eslint.js");
const oxlint = path.join(toolRoot, "node_modules/oxlint/bin/oxlint");
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const normalize = (file) => (path.isAbsolute(file) ? path.relative(frontend, file) : file).replaceAll("\\", "/").replace(/^\.\//, "");
const same = (left, right) => JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
const completed = (result) => !result.error && !result.signal && [0, 1].includes(result.status);
const env = { ...process.env, NO_COLOR: "1", FORCE_COLOR: "0" };
const lib = "client/src/lib/";
const components = "client/src/components/";
// Copied verbatim; checked against the original study table before execution.
const cases = [
  ["unused-variable", lib, "const unused = 1; export {};", ["@typescript-eslint/no-unused-vars"]],
  ["underscore-exemption", lib, "export function example(_first: number, used: number, _last: number) { const _unused = 1; return used; }", []],
  ["constant-if", lib, "export function example() { if (true) return 1; return 2; }", ["no-constant-condition"]],
  ["constant-loop-exemption", lib, "export function example() { while (true) { break; } }", []],
  ["unsafe-optional", lib, "export function example(value?: { callback(): void }) { (value?.callback)(); }", ["no-unsafe-optional-chaining"]],
  ["optional-as-assertion", lib, 'export function example(value?: { headers: Record<string, string> }) { return (value?.headers as Record<string, string>)["token"]; }', []],
  ["useless-assignment", lib, "export function example() { let value = 0; value = 1; return value; }", ["no-useless-assignment"]],
  ["unused-expression", lib, "export function example() { 42; }", ["@typescript-eslint/no-unused-expressions"]],
  ["unassigned-variable", lib, "let value; export function example() { return value; }", ["no-unassigned-vars"]],
  ["banned-ts-comment", lib, "// @ts-ignore\nexport const value = 1;", ["@typescript-eslint/ban-ts-comment"]],
  ["empty-object-type", lib, "export type Example = {};", ["@typescript-eslint/no-empty-object-type"]],
  ["array-constructor", lib, "export const value = new Array(1, 2);", ["@typescript-eslint/no-array-constructor"]],
  ["duplicate-enum", lib, "export enum Example { First = 1, Second = 1 }", ["@typescript-eslint/no-duplicate-enum-values"]],
  ["non-null-optional", lib, "export function example(value?: { other?: string }) { return value?.other!; }", ["@typescript-eslint/no-non-null-asserted-optional-chain"]],
  ["namespace", lib, "export namespace Example { export const value = 1; }", ["@typescript-eslint/no-namespace"]],
  ["wrapper-type", lib, "export type Example = String;", ["@typescript-eslint/no-wrapper-object-types"]],
  ["any-allowed", lib, "export function example(value: any) { return value; }", []],
  ["async-promise-executor", lib, "export const example = new Promise(async (resolve) => { resolve(1); });", ["no-async-promise-executor"]],
  ["debugger", lib, "export function example() { debugger; }", ["no-debugger"]],
  ["empty-catch", lib, "export function example() { try { JSON.parse('x'); } catch {} }", ["no-empty"]],
  ["constant-binary", lib, "export function example(value: number) { return (value < 1) ?? false; }", ["no-constant-binary-expression"]],
  ["preserve-caught-error", lib, "export function example() { try { JSON.parse('x'); } catch (error) { console.log(error); throw new Error('oops'); } }", ["preserve-caught-error"]],
  ["hooks-order", components, 'import { useState } from "react"; export function Example({ active }: { active: boolean }) { if (active) { useState(0); } return null; }', ["react-hooks/rules-of-hooks"]],
  ["hooks-dependencies", components, 'import { useEffect } from "react"; export function Example({ value }: { value: number }) { useEffect(() => { console.log(value); }, []); return null; }', ["react-hooks/exhaustive-deps"]],
  ["refresh-enabled", components, "export function Example() { return <div />; } export const extra = {};", ["react-refresh/only-export-components"]],
  ["refresh-constant", components, "export function Example() { return <div />; } export const extra = 1;", []],
  ["refresh-ui-exception", `${components}ui/`, "export function Example() { return <div />; } export const extra = {};", []],
  ["refresh-lib-exception", lib, "export function Example() { return <div />; } export const extra = {};", []],
  ["refresh-theme-exception", components, "export function Example() { return <div />; } export const extra = {};", [], "client/src/components/theme-provider.tsx"],
];

const report = {
  startedAt: new Date().toISOString(), node: process.version, platform: process.platform,
  sourceRevision: process.env.SCRIBER_SOURCE_REVISION ?? "d061ad75a0b39ca1b299eb8174ac828b122ef136",
  frontend, toolRoot, corpus: cases, corpusSha256: digest(JSON.stringify(cases)),
  cases: [], scopes: [], failures: [], summaries: [], passed: false,
};
const save = () => fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2) + "\n");
const inputs = ["package.json", "package-lock.json", "eslint.config.js", "eslint.residual.config.js", ".oxlintrc.json", ".oxlintrc.residual.json"];
const inputHashes = () => Object.fromEntries(inputs.map((name) => [name, digest(fs.readFileSync(path.join(frontend, name)))]));
const sourceSnapshot = () => {
  const entries = fs.globSync("client/src/**/*", { cwd: frontend }).filter((file) => fs.statSync(path.join(frontend, file)).isFile()).map((file) => [normalize(file), digest(fs.readFileSync(path.join(frontend, file)))]).sort((left, right) => left[0].localeCompare(right[0]));
  return { files: entries.length, sha256: digest(JSON.stringify(entries)) };
};
const owned = new Map();
const ownFile = (relative, source) => {
  const filename = path.join(frontend, relative);
  fs.mkdirSync(path.dirname(filename), { recursive: true });
  fs.writeFileSync(filename, source, { flag: "wx" });
  owned.set(filename, source);
  return filename;
};
const removeOwned = (filename) => {
  if (!owned.has(filename)) return;
  if (fs.existsSync(filename)) {
    if (fs.readFileSync(filename, "utf8") !== owned.get(filename)) throw new Error("Refusing to remove concurrently modified probe: " + filename);
    fs.rmSync(filename);
  }
  owned.delete(filename);
};
const run = (label, args) => {
  const result = spawnSync(process.execPath, args, { cwd: frontend, env, encoding: "utf8", maxBuffer: 16 * 1024 * 1024, timeout: 120000 });
  fs.writeFileSync(path.join(output, label + ".stdout.log"), result.stdout ?? "");
  fs.writeFileSync(path.join(output, label + ".stderr.log"), result.stderr ?? "");
  return { result, evidence: { command: [process.execPath, ...args], exitCode: result.status, signal: result.signal, error: result.error?.message ?? null, stdout: label + ".stdout.log", stderr: label + ".stderr.log" } };
};
const normalizeRule = (rule) => {
  const match = /^([^()]+)\((.+)\)$/.exec(rule ?? "");
  if (!match) return rule ?? "parse-error";
  const [, plugin, name] = match;
  if (plugin === "typescript") return "@typescript-eslint/" + name;
  if (plugin === "eslint") return ({ "no-unused-vars": "@typescript-eslint/no-unused-vars", "no-unused-expressions": "@typescript-eslint/no-unused-expressions", "no-array-constructor": "@typescript-eslint/no-array-constructor" })[name] ?? name;
  if (plugin === "react" && ["rules-of-hooks", "exhaustive-deps"].includes(name)) return "react-hooks/" + name;
  if (plugin === "react" && name === "only-export-components") return "react-refresh/only-export-components";
  return plugin + "/" + name;
};
const diagnosticRun = (label, kind, args) => {
  const { result, evidence } = run(label, args);
  const record = { ...evidence, validInvocation: completed(result), diagnostics: [], rules: [] };
  try {
    const data = JSON.parse(result.stdout);
    if (kind === "eslint") {
      if (!Array.isArray(data)) throw new Error("Expected ESLint result array");
      record.files = data.map((entry) => normalize(entry.filePath)).sort();
      record.diagnostics = data.flatMap((entry) => entry.messages.map((message) => ({ file: normalize(entry.filePath), rule: message.ruleId ?? "parse-error", message: message.message, severity: message.severity })));
      if (data.some((entry) => entry.messages.some((message) => message.fatal))) record.validInvocation = false;
    } else {
      if (!Array.isArray(data.diagnostics)) throw new Error("Expected Oxlint diagnostics array");
      record.numberOfFiles = data.number_of_files ?? null;
      record.diagnostics = data.diagnostics.map((message) => ({ file: normalize(message.filename ?? ""), rule: normalizeRule(message.code), rawRule: message.code, message: message.message, severity: message.severity }));
      if (record.numberOfFiles === 0) record.validInvocation = false;
      // Every enabled native rule belongs to these configured plugins. Parser
      // or configuration diagnostics have no matching registered-rule code and
      // must not be mistaken for ordinary rule-parity differences.
      if (data.diagnostics.some((message) => !/^(?:(?:eslint|typescript|react)\([^()]+\)|react-hooks\((?:rules-of-hooks|exhaustive-deps)\))$/.test(message.code ?? ""))) record.validInvocation = false;
    }
    record.rules = record.diagnostics.map((diagnostic) => diagnostic.rule).sort();
    if (record.rules.some((rule) => rule === "parse-error" || /(?:parse|syntax|config)[_-]?error/i.test(rule))) record.validInvocation = false;
    const expectedStatus = record.diagnostics.length ? 1 : 0;
    if (result.status !== expectedStatus) record.validInvocation = false;
  } catch (error) {
    record.parseError = error.message;
    record.validInvocation = false;
  }
  return record;
};
const eslintRun = (label, file, config = "eslint.config.js") => {
  const record = diagnosticRun(label, "eslint", [eslint, file, "--config", config, "--max-warnings", "0", "--format", "json"]);
  if (file !== "." && !same(record.files ?? [], [normalize(file)])) record.validInvocation = false;
  return record;
};
const nativeRun = (label, file, config) => {
  const record = diagnosticRun(label, "oxlint", [oxlint, "--config", config, "--max-warnings", "0", "--format", "json", file]);
  if (record.numberOfFiles !== 1) record.validInvocation = false;
  if (record.diagnostics.some((diagnostic) => diagnostic.file !== normalize(file))) record.validInvocation = false;
  return record;
};
const nativeConfigs = { "oxlint-pure": ".oxlintrc.json", "oxlint-retained-react": ".oxlintrc.migration-retained-react.json", "hybrid": ".oxlintrc.residual.json" };
const reactEslintConfig = "eslint.migration-react.config.js";
const recordCandidate = (id, candidate, expected, parts) => {
  const actual = parts.flatMap((part) => part.rules).sort();
  const validInvocation = parts.every((part) => part.validInvocation);
  // Both parts run independently for complete diagnostic evidence. This is the
  // real status an && chain of their recorded CLI processes would propagate.
  const exitCode = parts.find((part) => part.exitCode !== 0)?.exitCode ?? 0;
  const expectedExitCode = expected.length ? 1 : 0;
  const record = { id, candidate, expected: [...expected].sort(), actual, expectedExitCode, exitCode, validInvocation, parts, pass: validInvocation && exitCode === expectedExitCode && same(actual, expected) };
  report.cases.push(record);
  save();
};
const scopeCheck = (label, expectedFiles, expectedErrorRules) => {
  const baseline = eslintRun(label + "-eslint", ".");
  const baselinePass = baseline.validInvocation && same(baseline.files ?? [], expectedFiles) && same(baseline.rules, expectedErrorRules);
  report.scopes.push({ id: label, candidate: "eslint-baseline", files: baseline.files, pass: baselinePass, process: baseline });
  for (const [candidate, config] of Object.entries(nativeConfigs)) {
    const { result, evidence } = run(label + "-" + candidate, [oxlint, ...oxlintArgs(frontend, config, ["--debug", "files"])]);
    const files = (result.stdout ?? "").trim().split(/\r?\n/).filter(Boolean).map(normalize).sort();
    report.scopes.push({ id: label, candidate, files, pass: completed(result) && result.status === 0 && expectedFiles.length > 0 && same(files, expectedFiles), process: evidence });
  }
  save();
};

try {
  if (process.version !== "v26.5.0") throw new Error("Use pinned Node26.5.0");
  const originalCorpusSource = fs.readFileSync(path.join(probeRoot, "lint-parity.mjs"), "utf8").replaceAll("\r\n", "\n");
  const ownSource = fs.readFileSync(fileURLToPath(import.meta.url), "utf8").replaceAll("\r\n", "\n");
  const caseTable = /const cases = (\[[\s\S]*?\n\]);/;
  if (caseTable.exec(originalCorpusSource)?.[1] !== caseTable.exec(ownSource)?.[1] || cases.length !== 29 || new Set(cases.map((entry) => entry[0])).size !== 29) throw new Error("Migration comparison must use the exact original29-case corpus");
  report.originalCorpusSourceSha256 = digest(originalCorpusSource);
  report.harnessSha256 = digest(ownSource);
  report.inputHashesBefore = inputHashes();
  report.sourceBefore = sourceSnapshot();
  report.versions = { eslint: JSON.parse(fs.readFileSync(path.join(frontend, "node_modules/eslint/package.json"), "utf8")).version, oxlint: JSON.parse(fs.readFileSync(path.join(toolRoot, "node_modules/oxlint/package.json"), "utf8")).version };
  const pure = JSON.parse(fs.readFileSync(path.join(frontend, ".oxlintrc.json"), "utf8"));
  const removeReact = (configuration) => {
    const result = { ...configuration };
    if (result.plugins) result.plugins = result.plugins.filter((plugin) => plugin !== "react");
    if (result.rules) result.rules = Object.fromEntries(Object.entries(result.rules).filter(([rule]) => !rule.startsWith("react/")));
    if (result.overrides) result.overrides = result.overrides.map(removeReact).filter((override) => !override.rules || Object.keys(override.rules).length > 0);
    return result;
  };
  const retainedNative = JSON.stringify(removeReact(pure), null, 2) + "\n";
  const retainedReact = 'import baseline from "./eslint.config.js";\nexport default baseline.map((configuration) => configuration.rules ? { ...configuration, rules: Object.fromEntries(Object.entries(configuration.rules).filter(([name]) => name.startsWith("react-hooks/") || name === "react-refresh/only-export-components")) } : configuration);\n';
  ownFile(nativeConfigs["oxlint-retained-react"], retainedNative);
  ownFile(reactEslintConfig, retainedReact);
  fs.writeFileSync(path.join(output, "retained-react.oxlintrc.json"), retainedNative);
  fs.writeFileSync(path.join(output, "retained-react.eslint.config.js"), retainedReact);
  const originalFiles = lintFiles(frontend).map(normalize);
  const tracked = spawnSync("git", ["ls-tree", "--full-tree", "-r", "--name-only", report.sourceRevision, "Frontend"], { cwd: frontend, encoding: "utf8" });
  if (tracked.error || tracked.signal || tracked.status !== 0) throw new Error("Unable to enumerate frozen tracked source");
  const trackedFiles = tracked.stdout.trim().split(/\r?\n/).map((file) => file.replace(/^Frontend\//, ""));
  const trackedLintFiles = trackedFiles.filter((file) => /\.(?:js|mjs|cjs|ts|tsx)$/.test(file) && !/^(?:dist|node_modules|output|src-tauri|client\/public)\//.test(file));
  if (trackedFiles.length !== 353 || trackedLintFiles.length !== 251 || !same(originalFiles, [...trackedLintFiles, "eslint.residual.config.js", reactEslintConfig])) throw new Error("The complete frozen251-file source inventory must be present");
  report.scopeDefinition = "Original ESLint file scope, including its default mjs/cjs discovery and all original global/default ignores; explicit native paths avoid the parent tmp gitignore.";
  scopeCheck("complete-source", originalFiles, []);
  const scopeFixtures = [
    ["client/src/lib/__migration_scope_probe.ts", "debugger;\nexport {};\n", true],
    ["client/src/lib/__migration_scope_probe.mjs", "export {};\n", true],
    ["client/src/lib/__migration_scope_probe.cjs", "module.exports = {};\n", true],
    ...["dist", "node_modules", "output", "src-tauri", "client/public", "client/src/__migration_scope_nested/node_modules"].map((directory) => [directory + "/__migration_scope_probe/invalid.ts", "const = intentionally invalid;\n", false]),
  ];
  try {
    for (const [file, source] of scopeFixtures) ownFile(file, source);
    const expectedFiles = [...originalFiles, ...scopeFixtures.filter((entry) => entry[2]).map((entry) => entry[0])].sort();
    report.scopeFixtures = scopeFixtures.map(([file, , included]) => ({ file, included }));
    scopeCheck("new-source-and-ignored-errors", expectedFiles, ["no-debugger"]);
  } finally {
    for (const [file] of scopeFixtures) removeOwned(path.join(frontend, file));
  }

  for (const [id, directory, source, expected, explicitPath] of cases) {
    const relative = explicitPath ?? directory + "__migration_" + id.replaceAll("-", "_") + ".tsx";
    const filename = path.join(frontend, relative);
    if (!explicitPath && fs.existsSync(filename)) throw new Error("Preexisting corpus probe: " + filename);
    const backup = fs.existsSync(filename) ? fs.readFileSync(filename) : null;
    const written = source + "\n";
    fs.writeFileSync(filename, written);
    try {
      recordCandidate(id, "eslint-baseline", expected, [eslintRun(id + "-baseline", relative)]);
      recordCandidate(id, "oxlint-pure", expected, [nativeRun(id + "-pure", relative, nativeConfigs["oxlint-pure"])]);
      recordCandidate(id, "oxlint-retained-react", expected, [nativeRun(id + "-retained-native", relative, nativeConfigs["oxlint-retained-react"]), eslintRun(id + "-retained-react", relative, reactEslintConfig)]);
      recordCandidate(id, "hybrid", expected, [nativeRun(id + "-hybrid-native", relative, nativeConfigs.hybrid), eslintRun(id + "-hybrid-residual", relative, "eslint.residual.config.js")]);
    } finally {
      if (fs.readFileSync(filename, "utf8") !== written) throw new Error("Corpus fixture changed concurrently: " + filename);
      if (backup) fs.writeFileSync(filename, backup); else fs.rmSync(filename);
    }
  }
} catch (error) {
  report.failures.push(error.stack ?? error.message);
} finally {
  for (const filename of [...owned.keys()]) {
    try { removeOwned(filename); } catch (error) { report.failures.push(error.message); }
  }
  try {
    report.inputHashesAfter = inputHashes();
    report.sourceAfter = sourceSnapshot();
    report.restored = owned.size === 0 && JSON.stringify(report.inputHashesBefore) === JSON.stringify(report.inputHashesAfter) && JSON.stringify(report.sourceBefore) === JSON.stringify(report.sourceAfter);
  } catch (error) {
    report.restored = false;
    report.failures.push(error.message);
  }
  for (const candidate of ["eslint-baseline", "oxlint-pure", "oxlint-retained-react", "hybrid"]) {
    const rows = report.cases.filter((entry) => entry.candidate === candidate);
    const scopes = report.scopes.filter((entry) => entry.candidate === candidate);
    report.summaries.push({
      candidate, cases: rows.length, passedCases: rows.filter((entry) => entry.pass).length,
      evaluated: rows.length === 29 && new Set(rows.map((entry) => entry.id)).size === 29 && rows.every((entry) => entry.validInvocation),
      scopePassed: scopes.length === 2 && scopes.every((entry) => entry.pass),
      eligible: rows.length === 29 && rows.every((entry) => entry.pass) && scopes.length === 2 && scopes.every((entry) => entry.pass),
      mismatches: rows.filter((entry) => !entry.pass).map(({ id, expected, actual, expectedExitCode, exitCode, validInvocation }) => ({ id, expected, actual, expectedExitCode, exitCode, validInvocation })),
    });
  }
  report.harnessValid = report.failures.length === 0 && report.restored && report.summaries.every((entry) => entry.evaluated && entry.scopePassed);
  report.passed = report.harnessValid && report.summaries.filter((entry) => ["eslint-baseline", "hybrid"].includes(entry.candidate)).every((entry) => entry.eligible);
  report.finishedAt = new Date().toISOString();
  save();
  console.log(JSON.stringify({ passed: report.passed, harnessValid: report.harnessValid, restored: report.restored, summaries: report.summaries, failures: report.failures }, null, 2));
  if (!report.passed) process.exitCode = 1;
}

