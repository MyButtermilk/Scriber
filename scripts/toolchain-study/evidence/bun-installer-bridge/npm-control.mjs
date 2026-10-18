import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

const base = import.meta.dirname;
const directory = path.join(base, 'npm-drift-range');
const node = '/workspace/scratch/1ff238d8a5f3/toolchain/node_modules/.bin/node';
const npmCli = '/workspace/scratch/1ff238d8a5f3/toolchain/node_modules/npm/bin/npm-cli.js';
fs.mkdirSync(directory);
for (const name of ['package.json', 'package-lock.json']) {
  fs.copyFileSync(path.join(base, 'original', name), path.join(directory, name));
}
const manifestPath = path.join(directory, 'package.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
manifest.devDependencies.eslint = '^10.8.0';
fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
const hash = name => {
  const file = path.join(directory, name);
  return fs.existsSync(file) ? createHash('sha256').update(fs.readFileSync(file)).digest('hex') : null;
};
const snapshot = () => Object.fromEntries(['package.json', 'package-lock.json', 'bun.lock', 'node_modules/.package-lock.json'].map(name => [name, hash(name)]));
const before = snapshot();
const args = [npmCli, 'ci', '--no-audit', '--no-fund', '--cache', path.join(base, 'npm-cache')];
const result = spawnSync(node, args, {
  cwd: directory,
  env: { ...process.env, PATH: path.dirname(node) + path.delimiter + process.env.PATH, NO_COLOR: '1', FORCE_COLOR: '0' },
  encoding: 'utf8', timeout: 120000, killSignal: 'SIGKILL', maxBuffer: 16 * 1024 * 1024,
  detached: process.platform !== 'win32',
});
fs.writeFileSync(path.join(base, 'npm-drift-range.log'), (result.stdout ?? '') + (result.stderr ?? ''));
if (result.error?.code === 'ETIMEDOUT') { try { process.kill(-result.pid, 'SIGKILL'); } catch {} }
const nodeModules = path.join(directory, 'node_modules');
const report = {
  node: process.version,
  npm: JSON.parse(fs.readFileSync(path.resolve(npmCli, '../../package.json'), 'utf8')).version,
  command: [node, ...args], cwd: directory,
  mutation: { dependency: 'eslint', original: '10.8.0', actual: '^10.8.0', locked: '10.8.0' },
  exitCode: result.error || result.signal ? null : result.status,
  error: result.error?.message ?? null, signal: result.signal,
  before, after: snapshot(),
  installedEslint: fs.existsSync(path.join(nodeModules, 'eslint/package.json')) ? JSON.parse(fs.readFileSync(path.join(nodeModules, 'eslint/package.json'), 'utf8')).version : null,
  metadata: fs.existsSync(nodeModules) ? fs.readdirSync(nodeModules).filter(name => name.startsWith('.')) : [],
  log: path.join(base, 'npm-drift-range.log'),
};
fs.writeFileSync(path.join(base, 'npm-control-results.json'), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report, null, 2));
if (result.error || result.signal) process.exitCode = 1;
