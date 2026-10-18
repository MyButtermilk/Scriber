import fs from "node:fs";
import path from "node:path";
const source = process.argv[2];
const destination = path.join(import.meta.dirname, "runtime-tools");
const manifest = {
  name: "scriber-toolchain-study-runtimes", version: "1.0.0", private: true,
  dependencies: { npm: "11.17.0", bun: "1.4.3", pnpm: "12.14.0", yaml: "2.8.1" },
};
const lock = JSON.parse(fs.readFileSync(path.join(source, "package-lock.json"), "utf8"));
lock.name = manifest.name;
lock.version = manifest.version;
lock.packages[""] = { name: manifest.name, version: manifest.version, dependencies: manifest.dependencies };
for (const key of Object.keys(lock.packages)) {
  if (key === "node_modules/node" || key === "node_modules/node-bin-setup" || key.startsWith("node_modules/node/")) delete lock.packages[key];
}
fs.writeFileSync(path.join(destination, "package.json"), `${JSON.stringify(manifest, null, 2)}\n`);
fs.writeFileSync(path.join(destination, "package-lock.json"), `${JSON.stringify(lock, null, 2)}\n`);
