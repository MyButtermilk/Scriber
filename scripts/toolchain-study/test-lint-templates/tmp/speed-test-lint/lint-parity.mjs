import { readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";

const frontend = path.resolve(import.meta.dirname, "../../Frontend");
const require = createRequire(path.join(frontend, "package.json"));
const { ESLint } = require("eslint");
const tools = process.env.SCRIBER_LINT_TOOLS ?? path.resolve(frontend, "../../lint-tools");
const baseline = new ESLint({ cwd: frontend, overrideConfigFile: path.join(frontend, "eslint.config.js") });
const residual = new ESLint({ cwd: frontend, overrideConfigFile: path.join(frontend, "eslint.residual.config.js") });
const lib = "client/src/lib/";
const components = "client/src/components/";
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
const normalizeRule = (rule) => {
  const match = /^([^()]+)\((.+)\)$/.exec(rule ?? "");
  if (!match) return rule;
  const [, plugin, name] = match;
  if (plugin === "typescript") return `@typescript-eslint/${name}`;
  if (plugin === "eslint") return ({ "no-unused-vars": "@typescript-eslint/no-unused-vars", "no-unused-expressions": "@typescript-eslint/no-unused-expressions", "no-array-constructor": "@typescript-eslint/no-array-constructor" })[name] ?? name;
  return `${plugin}/${name}`;
};
const evidence = [];
const backups = [];
try {
  for (const [id, directory, source, expected, explicitPath] of cases) {
    const filename = explicitPath ?? `${directory}__speed_lint_${id.replaceAll("-", "_")}.tsx`;
    const absolute = path.join(frontend, filename);
    backups.push([absolute, existsSync(absolute) ? readFileSync(absolute) : null]);
    writeFileSync(absolute, `${source}\n`);
    const [original] = await baseline.lintText(source, { filePath: absolute });
    const [retained] = await residual.lintText(source, { filePath: absolute });
    const native = spawnSync(process.execPath, [path.join(tools, "node_modules/oxlint/bin/oxlint"), "--config", ".oxlintrc.residual.json", "--format", "json", filename], { cwd: frontend, encoding: "utf8" });
    if (native.error || native.signal || ![0, 1].includes(native.status)) throw native.error ?? new Error(`Native lint failed: ${native.stderr}`);
    const nativeResult = JSON.parse(native.stdout);
    const originalRules = original.messages.map((message) => message.ruleId ?? "parse-error").sort();
    const candidateRules = [...retained.messages.map((message) => message.ruleId ?? "parse-error"), ...nativeResult.diagnostics.map((message) => normalizeRule(message.code))].sort();
    const wanted = [...expected].sort();
    evidence.push({ id, filename, expected: wanted, baseline: originalRules, candidate: candidateRules, pass: JSON.stringify(wanted) === JSON.stringify(originalRules) && JSON.stringify(originalRules) === JSON.stringify(candidateRules) });
    if (backups.at(-1)[1] === null) rmSync(absolute); else writeFileSync(absolute, backups.at(-1)[1]);
    backups.pop();
  }
} finally {
  for (const [absolute, content] of backups) {
    if (content === null) rmSync(absolute, { force: true }); else writeFileSync(absolute, content);
  }
}
writeFileSync(path.join(import.meta.dirname, "lint-parity-results.json"), JSON.stringify(evidence, null, 2) + "\n");
const failures = evidence.filter((item) => !item.pass);
console.log(JSON.stringify({ cases: evidence.length, passed: evidence.length - failures.length, failures }, null, 2));
if (failures.length) process.exitCode = 1;
