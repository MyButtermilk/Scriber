import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";
import { compact, host, options, run, summarize } from "./common.mjs";

const input = options({ repo: path.resolve(import.meta.dirname, "../.."), tools: "", output: "", rounds: "5", resume: "false" });
const repo = path.resolve(input.repo);
const tools = path.resolve(input.tools);
const output = path.resolve(input.output || path.join(repo, "tmp/toolchain-study/managers"));
const rounds = Number(input.rounds);
if (!Number.isInteger(rounds) || rounds < 5 || rounds > 15) throw new Error("Use 5 to 15 measurement rounds");
const npm = path.join(tools, "node_modules/npm/bin/npm-cli.js");
const bun = path.join(tools, "node_modules/bun/bin/bun.exe");
const pnpm = path.join(tools, "node_modules", `@pnpm/exe.${process.platform}-${process.arch}`, process.platform === "win32" ? "pnpm.exe" : "pnpm");
for (const file of [npm, bun, pnpm]) if (!existsSync(file)) throw new Error(`Missing pinned tool: ${file}`);
const logs = path.join(output, "logs");
mkdirSync(output, { recursive: true });
const baseline = path.join(repo, "Frontend");
if (process.version !== "v26.5.0") throw new Error(`Expected pinned Node v26.5.0, got ${process.version}`);
const sourceCheck = spawnSync("git", ["diff", "--exit-code", "--no-ext-diff", "d061ad75a0b39ca1b299eb8174ac828b122ef136", "--", "Frontend"], { cwd: repo });
if (sourceCheck.status !== 0 || sourceCheck.error || sourceCheck.signal) throw new Error("Frontend differs from the frozen baseline commit");
const frontendRequire = createRequire(path.join(baseline, "package.json"));
const toolsRequire = createRequire(path.join(tools, "package.json"));
const yaml = toolsRequire("yaml");
const typescript = frontendRequire("typescript");
const baselineLockHash = createHash("sha256").update(readFileSync(path.join(baseline, "package-lock.json"))).digest("hex");
const report = { host: host(), versions: { npm: "11.17.0", bun: "1.4.3", pnpm: "12.14.0" }, rounds, baselineLockHash, policy: "Same locked versions; esbuild install allowed; pnpm default side-effects cache retained. Cold and warm-cache remove node_modules before timing. Repeat measures the same frozen CI command: npm ci itself deletes/reinstalls node_modules, while Bun/pnpm can no-op; it is not an equal-work developer npm-install comparison. Cold means empty local package/metadata/state caches, not flushed OS or remote CDN caches.", setup: [], samples: [], correctness: {}, inventory: {}, summary: {} };
if (!["false", "true"].includes(input.resume)) throw new Error("resume must be true or false");
const previous = input.resume === "true" ? JSON.parse(readFileSync(path.join(output, "results.json"), "utf8")) : null;
if (previous) {
  for (const key of ["rounds", "baselineLockHash", "versions"]) if (JSON.stringify(previous[key]) !== JSON.stringify(report[key])) throw new Error(`Cannot resume changed ${key}`);
  for (const key of ["node", "platform", "arch", "release", "cpuModel", "availableParallelism", "cpuQuota"]) if (previous.host[key] !== report.host[key]) throw new Error(`Cannot resume on a changed host: ${key}`);
  const expected = [];
  for (let round = 0; round < rounds; round++) {
    const names = ["npm", "bun", "pnpm"];
    for (const name of names.slice(round % 3).concat(names.slice(0, round % 3))) for (const scenario of ["cold", "warm-cache", "repeat"]) expected.push(`${name}-${scenario}-${round}`);
  }
  if (previous.samples.length % 3 !== 0 || previous.samples.length > expected.length || previous.samples.some((sample, index) => sample.label !== expected[index] || !sample.passed)) throw new Error("Resume requires a successful prefix of complete three-scenario groups");
  report.samples = previous.samples;
  report.continuations = [...(previous.continuations ?? []), { at: new Date().toISOString(), completedSamples: previous.samples.length, reason: "Resume between complete manager rounds; every remaining cold lane has a fresh unique cache" }];
}
const save = () => writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2));
const env = { PATH: `${path.join(tools, "node_modules/.bin")}${path.delimiter}${path.dirname(process.execPath)}${path.delimiter}${process.env.PATH}`, npm_config_update_notifier: "false" };
const dirs = Object.fromEntries(["npm", "bun", "pnpm"].map(name => [name, path.join(output, "projects", name)]));
for (const [name, expected] of Object.entries(report.versions)) {
  const observed = JSON.parse(readFileSync(path.join(tools, "node_modules", name, "package.json"), "utf8")).version;
  if (observed !== expected) throw new Error(`${name} version ${observed} differs from ${expected}`);
}
report.lockHashes = {};
report.lockArtifacts = {};
const artifactIndexes = {};
const bundledProofs = new Set();

for (const name of Object.keys(dirs)) {
  const directory = dirs[name];
  rmSync(directory, { recursive: true, force: true });
  cpSync(baseline, directory, { recursive: true, filter: source => !["node_modules", "dist", "output", "src-tauri"].includes(path.relative(baseline, source).split(path.sep)[0]) });
  if (name === "bun") {
    const manifest = JSON.parse(readFileSync(path.join(directory, "package.json"), "utf8"));
    manifest.trustedDependencies = ["esbuild"];
    writeFileSync(path.join(directory, "package.json"), `${JSON.stringify(manifest, null, 2)}\n`);
    const migrated = run(bun, ["install", "--lockfile-only"], { cwd: directory, output: logs, label: "setup-bun", env });
    report.setup.push(compact(migrated));
    if (!migrated.passed) { save(); throw new Error("Bun lock migration failed"); }
    rmSync(path.join(directory, "package-lock.json"));
  }
  if (name === "pnpm") {
    const workspacePath = path.join(directory, "pnpm-workspace.yaml");
    const lockPath = path.join(directory, "pnpm-lock.yaml");
    const buildPolicy = "allowBuilds:\n  'esbuild@0.28.1': true\n";
    writeFileSync(workspacePath, buildPolicy);
    const migrated = run(pnpm, ["import"], { cwd: directory, output: logs, label: "setup-pnpm-naive", env });
    report.setup.push(compact(migrated));
    if (!migrated.passed) { save(); throw new Error("pnpm lock migration failed"); }
    const naiveText = readFileSync(lockPath, "utf8");
    writeFileSync(path.join(logs, "naive-pnpm-lock.yaml"), naiveText);
    const naive = yaml.parse(naiveText);
    const selector = "cmdk@1.1.1>@radix-ui/react-primitive";
    const peers = "(@types/react-dom@19.2.2(@types/react@19.2.2))(@types/react@19.2.2)(react-dom@19.2.0(react@19.2.0))(react@19.2.0)";
    const cmdkSnapshot = `cmdk@1.1.1${peers}`;
    const expectedEdge = `2.1.3${peers}`;
    const targetSnapshot = `@radix-ui/react-primitive@${expectedEdge}`;
    const naiveEdge = naive.snapshots?.[cmdkSnapshot]?.dependencies?.["@radix-ui/react-primitive"];
    report.pnpmImportCorrection = {
      rationale: "Preserve cmdk@1.1.1's original npm resolution to @radix-ui/react-primitive@2.1.3. Unscoped pnpm import selected another already-locked version, 2.1.4. The supported parent-scoped override restores this edge without upgrading dependencies; installed native graph and integrity gates still apply.",
      documentation: "https://pnpm.io/settings/dependency-resolution#overrides",
      selector, version: "2.1.3", snapshot: cmdkSnapshot,
      naiveEdge, expectedEdge,
      naiveLockEvidence: "logs/naive-pnpm-lock.yaml",
      naiveLockSha256: createHash("sha256").update(naiveText).digest("hex"),
    };
    save();
    if (![expectedEdge, `2.1.4${peers}`].includes(naiveEdge) || !naive.snapshots?.[targetSnapshot]) throw new Error("Unexpected naive pnpm cmdk edge or missing original 2.1.3 peer context");
    // Re-import from package-lock.json with a supported scoped override.
    // Keep the unmodified first import as evidence, outside all timings.
    writeFileSync(workspacePath, `${buildPolicy}overrides:\n  '${selector}': 2.1.3\n`);
    rmSync(lockPath);
    const correctedImport = run(pnpm, ["import"], { cwd: directory, output: logs, label: "setup-pnpm-preserve-cmdk", env });
    report.setup.push(compact(correctedImport));
    if (!correctedImport.passed) { save(); throw new Error("pnpm import with scoped version preservation failed"); }
    const correctedText = readFileSync(lockPath, "utf8");
    writeFileSync(path.join(logs, "corrected-pnpm-lock.yaml"), correctedText);
    const corrected = yaml.parse(correctedText);
    const correctedEdge = corrected.snapshots?.[cmdkSnapshot]?.dependencies?.["@radix-ui/react-primitive"];
    Object.assign(report.pnpmImportCorrection, {
      correctedEdge, correctedLockEvidence: "logs/corrected-pnpm-lock.yaml",
      correctedLockSha256: createHash("sha256").update(correctedText).digest("hex"),
    });
    save();
    if (correctedEdge !== expectedEdge || corrected.overrides?.[selector] !== "2.1.3") throw new Error("pnpm import did not preserve the exact original cmdk resolution and scoped override");
    if (JSON.stringify(corrected.snapshots?.[targetSnapshot]) !== JSON.stringify(naive.snapshots[targetSnapshot])) throw new Error("pnpm import changed the original 2.1.3 peer context");
    const originalIntegrity = naive.packages?.["@radix-ui/react-primitive@2.1.3"]?.resolution?.integrity;
    if (!originalIntegrity || corrected.packages?.["@radix-ui/react-primitive@2.1.3"]?.resolution?.integrity !== originalIntegrity) throw new Error("pnpm import changed the original 2.1.3 tarball integrity");
    rmSync(path.join(directory, "package-lock.json"));
  }
  const lockName = { npm: "package-lock.json", bun: "bun.lock", pnpm: "pnpm-lock.yaml" }[name];
  report.lockHashes[name] = createHash("sha256").update(readFileSync(path.join(directory, lockName))).digest("hex");
  const lockText = readFileSync(path.join(directory, lockName), "utf8");
  let artifacts;
  let externalArtifacts;
  if (name === "npm") {
    const packages = JSON.parse(lockText).packages;
    const packageId = (key, value) => `${value.name ?? key.split("node_modules/").at(-1)}@${value.version}`;
    artifacts = Object.entries(packages).filter(([key]) => key !== "").map(([key, value]) => `${packageId(key, value)}|${value.integrity}`);
    externalArtifacts = Object.entries(packages).filter(([key, value]) => key && !value.inBundle).map(([key, value]) => [packageId(key, value), value.integrity]);
    for (const [key, value] of Object.entries(packages)) if (value.inBundle) {
      let owner = key;
      do {
        const boundary = owner.lastIndexOf("/node_modules/");
        if (boundary < 0) throw new Error(`Bundled package has no locked tarball owner: ${key}`);
        owner = owner.slice(0, boundary);
      } while (packages[owner]?.inBundle);
      if (!packages[owner]?.integrity) throw new Error(`Bundled package owner has no integrity: ${key}`);
      bundledProofs.add(`${packageId(key, value)}|${packageId(owner, packages[owner])}|${packages[owner].integrity}`);
    }
  } else if (name === "bun") {
    const parsed = typescript.parseConfigFileTextToJson(lockName, lockText);
    if (parsed.error) throw new Error("Cannot parse imported Bun lockfile");
    artifacts = Object.values(parsed.config.packages).map(value => `${value[0]}|${value.at(-1)}`);
    externalArtifacts = Object.values(parsed.config.packages).map(value => [value[0], value.at(-1)]);
  } else {
    const packages = yaml.parse(lockText).packages;
    artifacts = Object.entries(packages).map(([key, value]) => `${key}|${value.resolution.integrity}`);
    externalArtifacts = Object.entries(packages).map(([key, value]) => [key, value.resolution.integrity]);
  }
  report.lockArtifacts[name] = [...new Set(artifacts)].sort();
  artifactIndexes[name] = new Map();
  for (const [id, integrity] of externalArtifacts) if (typeof integrity === "string" && integrity.length) {
    const values = artifactIndexes[name].get(id) ?? new Set();
    values.add(integrity.trim().split(/\s+/).sort().join(" "));
    artifactIndexes[name].set(id, values);
  }
  if (previous && previous.lockHashes[name] !== report.lockHashes[name]) throw new Error(`Cannot resume a changed ${name} lockfile`);
}

function installation(name, cache, label) {
  const commands = {
    npm: [process.execPath, [npm, "ci", "--no-audit", "--no-fund", "--cache", cache]],
    bun: [bun, ["install", "--frozen-lockfile", "--cache-dir", cache]],
    pnpm: [pnpm, ["install", "--frozen-lockfile", "--store-dir", path.join(cache, "store"), `--config.cache-dir=${path.join(cache, "metadata")}`, "--state-dir", path.join(cache, "state"), "--reporter=append-only"]],
  };
  const [command, args] = commands[name];
  return run(command, args, { cwd: dirs[name], output: logs, label, env });
}

for (let round = 0; round < rounds; round++) {
  const names = ["npm", "bun", "pnpm"];
  const order = names.slice(round % names.length).concat(names.slice(0, round % names.length));
  for (const name of order) {
    if (report.samples.some(sample => sample.manager === name && sample.round === round && sample.scenario === "repeat")) continue;
    const cache = path.join(output, "caches", `${name}-${round}`);
    rmSync(cache, { recursive: true, force: true });
    for (const scenario of ["cold", "warm-cache", "repeat"]) {
      if (scenario !== "repeat") rmSync(path.join(dirs[name], "node_modules"), { recursive: true, force: true });
      const result = installation(name, cache, `${name}-${scenario}-${round}`);
      report.samples.push({ manager: name, scenario, round, ...compact(result) });
      save();
    }
    // Each round has its own cache. Retain completed caches until the study is
    // over: pnpm can still finalize metadata after its installer has exited.
  }
}

function inventory(directory) {
  const packages = new Set();
  const visited = new Set();
  const walk = root => {
    if (!existsSync(root)) return;
    const real = realpathSync(root);
    if (visited.has(real)) return;
    visited.add(real);
    for (const child of readdirSync(root, { withFileTypes: true })) {
      const file = path.join(root, child.name);
      if (child.name === ".pnpm") {
        for (const entry of readdirSync(file, { withFileTypes: true })) if (entry.isDirectory()) walk(path.join(file, entry.name, "node_modules"));
      } else if (child.name.startsWith("@")) {
        walk(file);
      } else if (!child.name.startsWith(".")) {
        const manifest = path.join(file, "package.json");
        if (!existsSync(manifest)) continue;
        const value = JSON.parse(readFileSync(manifest, "utf8"));
        packages.add(`${value.name}@${value.version}`);
        walk(path.join(file, "node_modules"));
      }
    }
  };
  walk(path.join(directory, "node_modules"));
  return [...packages].sort();
}

const nativeHost = {
  os: process.platform,
  cpu: process.arch,
  // Do not infer musl merely from the absence of glibc metadata.
  libc: process.platform === "linux" && process.report.getReport().header.glibcVersionRuntime ? "glibc" : null,
};
const referenceOptionalEdges = new Map();
function platformMismatch(manifest) {
  if (!manifest) return null;
  for (const [key, current] of Object.entries(nativeHost)) {
    const constraint = manifest[key];
    if (!current || !constraint) continue;
    const values = Array.isArray(constraint) ? constraint : [constraint];
    const positive = values.filter(value => !value.startsWith("!"));
    if (values.includes(`!${current}`) || (positive.length && !positive.includes(current) && !positive.includes("any"))) return `${key}:${current} excludes ${values.join(",")}`;
  }
  return null;
}

function dependencyGraph(directory, { native = false, captureReference = false } = {}) {
  const visited = new Set();
  const contexts = new Set();
  const packages = new Map();
  const skipped = new Set();
  const missingRequired = new Set();
  function resolvePackage(from, name) {
    let current = from;
    while (true) {
      const candidate = path.join(current, "node_modules", name);
      if (existsSync(path.join(candidate, "package.json"))) return realpathSync(candidate);
      const parent = path.dirname(current);
      if (parent === current) return null;
      current = parent;
    }
  }
  const identity = folder => {
    const manifest = JSON.parse(readFileSync(path.join(folder, "package.json"), "utf8"));
    const peers = Object.keys(manifest.peerDependencies ?? {}).sort().map(name => {
      const resolved = resolvePackage(folder, name);
      const peer = resolved ? JSON.parse(readFileSync(path.join(resolved, "package.json"), "utf8")) : null;
      return `${name}=${peer ? `${peer.name}@${peer.version}` : "missing"}`;
    });
    return `${manifest.name}@${manifest.version}[${peers.join(",")}]`;
  };
  const visit = (folder, isRoot = false) => {
    const absolute = realpathSync(folder);
    if (visited.has(absolute)) return;
    visited.add(absolute);
    const manifest = JSON.parse(readFileSync(path.join(absolute, "package.json"), "utf8"));
    if (!isRoot) packages.set(absolute, manifest);
    const edges = [];
    const sourceIdentity = isRoot ? "<root>" : identity(absolute);
    const dependencyGroups = {
      dependency: { ...manifest.dependencies, ...(isRoot ? manifest.devDependencies : {}) },
      optional: manifest.optionalDependencies ?? {},
      peer: manifest.peerDependencies ?? {},
    };
    // optionalDependencies overrides dependencies for an overlapping name.
    for (const name of Object.keys(dependencyGroups.optional)) delete dependencyGroups.dependency[name];
    for (const [kind, dependencies] of Object.entries(dependencyGroups)) for (const name of Object.keys(dependencies)) {
      const resolved = resolvePackage(absolute, name);
      const target = resolved ? JSON.parse(readFileSync(path.join(resolved, "package.json"), "utf8")) : null;
      const edgeKey = `${sourceIdentity}|${kind}:${name}`;
      if (captureReference && kind === "optional" && target) {
        const prior = referenceOptionalEdges.get(edgeKey);
        // An ambiguous baseline context cannot justify waiving a missing edge.
        referenceOptionalEdges.set(edgeKey, prior === null || (prior && JSON.stringify(prior) !== JSON.stringify(target)) ? null : target);
      }
      const mismatch = kind === "optional" ? platformMismatch(target ?? referenceOptionalEdges.get(edgeKey)) : null;
      if (native && mismatch) {
        edges.push(`${kind}:${name}=>platform-skipped`);
        skipped.add(`${edgeKey}|${mismatch}`);
        continue;
      }
      edges.push(`${kind}:${name}=>${resolved ? identity(resolved) : "missing"}`);
      if (!resolved && (kind === "dependency" || (kind === "peer" && !manifest.peerDependenciesMeta?.[name]?.optional))) missingRequired.add(edgeKey);
      if (resolved) visit(resolved);
    }
    contexts.add(`${sourceIdentity}|${edges.sort().join("|")}`);
  };
  visit(directory, true);
  return { contexts: [...contexts].sort(), packages, skipped: [...skipped].sort(), missingRequired: [...missingRequired].sort() };
}

function reachableArtifacts(name, graph) {
  const artifacts = new Set();
  const errors = [];
  const index = artifactIndexes[name];
  for (const [folder, manifest] of graph.packages) {
    const id = `${manifest.name}@${manifest.version}`;
    let bundled = null;
    // A bundled child is covered only by a reachable, explicitly declared
    // ancestor bundle whose exact tarball ownership is proven by npm's lock.
    let ancestor = path.dirname(folder);
    while (ancestor !== path.dirname(ancestor) && ancestor !== dirs[name]) {
      const owner = graph.packages.get(ancestor);
      if (owner) {
        const relative = path.relative(ancestor, folder).split(path.sep);
        const child = relative[1]?.startsWith("@") ? `${relative[1]}/${relative[2]}` : relative[1];
        const declared = owner.bundleDependencies ?? owner.bundledDependencies;
        const includesChild = declared === true || (Array.isArray(declared) && declared.includes(child));
        const ownerId = `${owner.name}@${owner.version}`;
        const integrities = index.get(ownerId);
        if (relative[0] === "node_modules" && includesChild && integrities?.size === 1) {
          const integrity = [...integrities][0];
          if (bundledProofs.has(`${id}|${ownerId}|${integrity}`)) bundled = `bundle:${id}|owner:${ownerId}|${integrity}`;
        }
      }
      ancestor = path.dirname(ancestor);
    }
    if (bundled) {
      artifacts.add(bundled);
      continue;
    }
    const integrities = index.get(id);
    if (integrities?.size !== 1) errors.push(`${id}: expected one standalone integrity or proven bundle owner; found ${integrities?.size ?? 0}`);
    else artifacts.add(`tarball:${id}|${[...integrities][0]}`);
  }
  return { artifacts: [...artifacts].sort(), errors: [...new Set(errors)].sort() };
}

report.nativeGate = { host: nativeHost, policy: "Compare installed reachable versions, dependency/peer resolutions and canonical tarball integrities on this native OS. Optional packages are excluded only when an installed manifest proves os/cpu/libc incompatibility. Bundled children require the npm lock's explicit parent-tarball proof. Raw inventory, graph and all-platform lock drift remain diagnostic evidence; this is not a claim of WASI or other-platform parity.", candidates: {} };
for (const name of Object.keys(dirs)) {
  report.inventory[name] = inventory(dirs[name]);
  report.inventory[`${name}Graph`] = dependencyGraph(dirs[name], { captureReference: name === "npm" }).contexts;
  const nativeGraph = dependencyGraph(dirs[name], { native: true });
  report.nativeGate.candidates[name] = {
    graph: nativeGraph.contexts,
    versions: [...new Set([...nativeGraph.packages.values()].map(value => `${value.name}@${value.version}`))].sort(),
    skippedByPlatform: nativeGraph.skipped,
    missingRequired: nativeGraph.missingRequired,
    ...reachableArtifacts(name, nativeGraph),
  };
  report.correctness[name] = [];
  for (const script of ["check", "lint", "test", "build"]) {
    const result = run(process.execPath, [npm, "run", script], { cwd: dirs[name], output: logs, label: `${name}-verify-${script}`, env, timeout: 600000 });
    report.correctness[name].push(compact(result));
    save();
  }
}
for (const name of ["bun", "pnpm"]) {
  report.inventory[`${name}Only`] = report.inventory[name].filter(item => !report.inventory.npm.includes(item));
  report.inventory[`${name}Missing`] = report.inventory.npm.filter(item => !report.inventory[name].includes(item));
  report.inventory[`${name}GraphOnly`] = report.inventory[`${name}Graph`].filter(item => !report.inventory.npmGraph.includes(item));
  report.inventory[`${name}GraphMissing`] = report.inventory.npmGraph.filter(item => !report.inventory[`${name}Graph`].includes(item));
  const candidate = report.nativeGate.candidates[name];
  const reference = report.nativeGate.candidates.npm;
  candidate.graphOnly = candidate.graph.filter(item => !reference.graph.includes(item));
  candidate.graphMissing = reference.graph.filter(item => !candidate.graph.includes(item));
  candidate.artifactsOnly = candidate.artifacts.filter(item => !reference.artifacts.includes(item));
  candidate.artifactsMissing = reference.artifacts.filter(item => !candidate.artifacts.includes(item));
}
report.allPlatformLockDrift = Object.fromEntries(["bun", "pnpm"].map(name => [name, {
  only: report.lockArtifacts[name].filter(item => !report.lockArtifacts.npm.includes(item)),
  missing: report.lockArtifacts.npm.filter(item => !report.lockArtifacts[name].includes(item)),
}]));
for (const name of Object.keys(dirs)) for (const scenario of ["cold", "warm-cache", "repeat"]) {
  report.summary[`${name}/${scenario}`] = summarize(report.samples.filter(sample => sample.manager === name && sample.scenario === scenario));
}
report.eligibility = {};
for (const name of Object.keys(dirs)) {
  const lockName = { npm: "package-lock.json", bun: "bun.lock", pnpm: "pnpm-lock.yaml" }[name];
  const lockUnchanged = report.lockHashes[name] === createHash("sha256").update(readFileSync(path.join(dirs[name], lockName))).digest("hex");
  const candidate = report.nativeGate.candidates[name];
  const reference = report.nativeGate.candidates.npm;
  const versionsMatch = JSON.stringify(candidate.versions) === JSON.stringify(reference.versions);
  const graphMatches = !candidate.missingRequired.length && JSON.stringify(candidate.graph) === JSON.stringify(reference.graph);
  const installs = report.samples.filter(sample => sample.manager === name);
  const allInstallsPassed = installs.length === rounds * 3 && installs.every(sample => sample.passed);
  const allChecksPassed = report.correctness[name].length === 4 && report.correctness[name].every(sample => sample.passed);
  const artifactsMatch = !candidate.errors.length && JSON.stringify(candidate.artifacts) === JSON.stringify(reference.artifacts);
  report.eligibility[name] = { lockUnchanged, versionsMatch, graphMatches, artifactsMatch, allInstallsPassed, allChecksPassed, eligible: lockUnchanged && versionsMatch && graphMatches && artifactsMatch && allInstallsPassed && allChecksPassed };
}
save();
console.log(JSON.stringify({ summary: report.summary, eligibility: report.eligibility }));
if (!report.eligibility.npm.eligible || Object.values(report.summary).some(value => !value.eligible)) process.exitCode = 1;
