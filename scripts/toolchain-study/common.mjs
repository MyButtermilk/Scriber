import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

export function options(defaults) {
  const values = { ...defaults };
  for (let index = 2; index < process.argv.length; index += 2) {
    const key = process.argv[index].replace(/^--/, "");
    if (!(key in values) || process.argv[index + 1] === undefined) throw new Error(`Invalid argument: ${key}`);
    values[key] = process.argv[index + 1];
  }
  return values;
}

export function host() {
  return {
    node: process.version, platform: process.platform, arch: process.arch,
    release: os.release(), cpus: os.cpus().length, availableParallelism: os.availableParallelism(),
    cpuModel: os.cpus()[0]?.model, memoryBytes: os.totalmem(),
    cpuQuota: existsSync("/sys/fs/cgroup/cpu.max") ? readFileSync("/sys/fs/cgroup/cpu.max", "utf8").trim() : null,
    sourceCommit: "d061ad75a0b39ca1b299eb8174ac828b122ef136",
    rssScope: process.platform === "linux" && existsSync(process.env.SCRIBER_TIME_EXE ?? "/usr/bin/time") ? "wait4 maximum resident set of command/descendants; not sum of concurrent peaks" : "unavailable",
  };
}

export function run(command, args, { cwd, output, label, env = {}, timeout = 300000 }) {
  mkdirSync(output, { recursive: true });
  const rssPath = path.join(output, `${label}.rss`);
  const timeExecutable = process.env.SCRIBER_TIME_EXE ?? "/usr/bin/time";
  const useTime = process.platform === "linux" && existsSync(timeExecutable);
  const started = performance.now();
  const result = spawnSync(useTime ? timeExecutable : command,
    useTime ? ["-f", "%M", "-o", rssPath, command, ...args] : args, {
      cwd, env: { ...process.env, CI: "true", NO_COLOR: "1", FORCE_COLOR: "0", ...env },
      encoding: "utf8", maxBuffer: 32 * 1024 * 1024, timeout,
    });
  const seconds = (performance.now() - started) / 1000;
  const stdout = result.stdout ?? "";
  const stderr = result.stderr ?? "";
  writeFileSync(path.join(output, `${label}.log`), `${stdout}\n${stderr}`);
  const rssText = useTime && existsSync(rssPath) ? readFileSync(rssPath, "utf8").trim().split("\n").at(-1) : "";
  const record = {
    label, command, args, seconds, status: result.status, signal: result.signal,
    error: result.error?.message ?? null,
    passed: result.status === 0 && !result.signal && !result.error,
    peakRssKiB: /^\d+$/.test(rssText) ? Number(rssText) : null,
  };
  console.log(JSON.stringify(record));
  if (result.error?.code === "ETIMEDOUT") throw new Error(`Timed command ${label} exceeded its bound; abort to avoid measuring with an orphaned descendant`);
  return { ...record, stdout, stderr };
}

export function compact(result) {
  const { stdout, stderr, ...record } = result;
  return record;
}

export function summarize(samples) {
  const sorted = samples.filter(sample => sample.passed).map(sample => sample.seconds).sort((a, b) => a - b);
  return {
    attempts: samples.length, passed: sorted.length,
    eligible: sorted.length === samples.length && sorted.length >= 5,
    medianSeconds: sorted.length === samples.length && sorted.length >= 5 ? (sorted[Math.floor((sorted.length - 1) / 2)] + sorted[Math.ceil((sorted.length - 1) / 2)]) / 2 : null,
    minSeconds: sorted[0] ?? null, maxSeconds: sorted.at(-1) ?? null,
    peakRssKiB: samples.some(sample => sample.peakRssKiB !== null) ? Math.max(...samples.map(sample => sample.peakRssKiB ?? 0)) : null,
  };
}
