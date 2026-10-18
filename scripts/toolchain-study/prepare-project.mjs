import fs from "node:fs";
import path from "node:path";
const repo = path.resolve(import.meta.dirname, "../..");
const name = process.argv[2];
if (!["compilers", "test-lint"].includes(name)) throw new Error("Unknown isolated project");
const destination = path.join(repo, "tmp/toolchain-study", `${name}-project`);
fs.rmSync(destination, { recursive: true, force: true });
fs.mkdirSync(destination, { recursive: true });
const source = path.join(repo, "Frontend");
fs.cpSync(source, path.join(destination, "Frontend"), {
  recursive: true, verbatimSymlinks: true,
  filter: file => !["dist", "output", "src-tauri"].includes(path.relative(source, file).split(path.sep)[0]),
});
if (name === "test-lint") {
  const templates = path.join(import.meta.dirname, "test-lint-templates");
  fs.cpSync(templates, destination, { recursive: true, verbatimSymlinks: true });
}
console.log(destination);
