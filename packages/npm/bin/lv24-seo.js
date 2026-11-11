#!/usr/bin/env node
/**
 * Thin launcher for the Python package `lv24-seo`.
 *
 * `npx @lv24/seo …` is a convenience entry point, not a second implementation: it forwards every
 * argument to `uvx --from lv24-seo==<npm-version> lv24-seo …` (or to an already installed
 * `lv24-seo` binary). Exit code, stdout and stderr are passed through unchanged so agents can
 * rely on `--json` and the documented exit codes.
 *
 * Recursion guard: this package itself installs a `lv24-seo` shim, which may shadow the real
 * Python binary in PATH. The fallback therefore resolves the binary through PATH and refuses
 * to exec its own file. `LV24_SEO_BIN` must be an absolute executable path and is validated
 * before use. Set `LV24_SEO_PIN=0` to opt back into the unpinned (`latest`) lookup.
 */
'use strict';

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const PACKAGE = 'lv24-seo';
const args = process.argv.slice(2);

function npmVersion() {
  try {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'),
    );
    if (typeof pkg.version === 'string' && /^\d+\.\d+\.\d+/.test(pkg.version)) {
      return pkg.version;
    }
  } catch {
    // fall through to unpinned
  }
  return null;
}

function uvxPrefix() {
  if (process.env.LV24_SEO_PIN === '0') {
    return ['--from', PACKAGE, PACKAGE];
  }
  const version = npmVersion();
  if (version) {
    return ['--from', `${PACKAGE}==${version}`, PACKAGE];
  }
  return ['--from', PACKAGE, PACKAGE];
}

/** Absolute path of this launcher file (symlinks resolved) for the recursion guard. */
function ownRealPath() {
  try {
    return fs.realpathSync(__filename);
  } catch {
    return path.resolve(__filename);
  }
}

/** First PATH entry named `lv24-seo` that is not this launcher itself. */
function findSystemBinary() {
  const pathEnv = process.env.PATH || '';
  const dirs = pathEnv.split(path.delimiter).filter(Boolean);
  const names =
    process.platform === 'win32' ? ['lv24-seo.exe', 'lv24-seo.cmd', 'lv24-seo'] : ['lv24-seo'];
  const self = ownRealPath();
  for (const dir of dirs) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      let stat = null;
      try {
        stat = fs.statSync(candidate);
      } catch {
        continue;
      }
      if (!stat.isFile()) {
        continue;
      }
      let real = candidate;
      try {
        real = fs.realpathSync(candidate);
      } catch {
        // compare literally
      }
      if (real === self || looksLikeOwnShim(candidate)) {
        continue; // our own shim: executing it would recurse
      }
      if (process.platform !== 'win32') {
        try {
          fs.accessSync(candidate, fs.constants.X_OK);
        } catch {
          continue;
        }
      }
      return candidate;
    }
  }
  return null;
}

/**
 * Detect an npm-generated wrapper (`.cmd`/`.ps1`/shell shim) that points back
 * at this launcher: it mentions our script path but is not the script itself.
 */
function looksLikeOwnShim(candidate) {
  let head = '';
  try {
    const fd = fs.openSync(candidate, 'r');
    try {
      const buffer = Buffer.alloc(4096);
      const read = fs.readSync(fd, buffer, 0, buffer.length, 0);
      head = buffer.subarray(0, read).toString('utf8');
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return false;
  }
  if (!head || head.includes('\0')) {
    return false; // binary: a real compiled binary, not our wrapper
  }
  return head.includes('lv24-seo.js') || head.includes('@lv24/seo');
}

/** How many launcher hops preceded this process (recursion backstop). */
function launcherDepth() {
  const raw = Number.parseInt(process.env.LV24_SEO_LAUNCHER_DEPTH || '0', 10);
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
}

function validatedOverride() {
  const override = process.env.LV24_SEO_BIN;
  if (!override) {
    return null;
  }
  const problems = [];
  if (!path.isAbsolute(override)) {
    problems.push('is not an absolute path');
  } else {
    let stat = null;
    try {
      stat = fs.statSync(override);
    } catch {
      problems.push('does not exist');
    }
    if (stat && !stat.isFile()) {
      problems.push('is not a file');
    }
    if (process.platform !== 'win32' && stat && stat.isFile()) {
      try {
        fs.accessSync(override, fs.constants.X_OK);
      } catch {
        problems.push('is not executable');
      }
    }
    if (stat && stat.isFile()) {
      let real = override;
      try {
        real = fs.realpathSync(override);
      } catch {
        // compare literally
      }
      if (real === ownRealPath() || looksLikeOwnShim(override)) {
        problems.push('points at the npm launcher itself (recursion)');
      }
    }
  }
  if (problems.length > 0) {
    process.stderr.write(`lv24-seo launcher: LV24_SEO_BIN ${problems.join(', ')}: ${override}\n`);
    process.exit(2);
  }
  return override;
}

function candidates() {
  const override = validatedOverride();
  if (override) {
    return [{ command: override, prefix: [] }];
  }
  const list = [{ command: 'uvx', prefix: uvxPrefix() }];
  const system = findSystemBinary();
  if (system) {
    list.push({ command: system, prefix: [] });
  }
  return list;
}

function hint() {
  return [
    'lv24-seo needs uv (recommended) or an installed lv24-seo binary.',
    '',
    '  Install uv:     https://docs.astral.sh/uv/getting-started/installation/',
    '  Or install the CLI directly:',
    '                  pipx install lv24-seo   (or: uv tool install lv24-seo)',
    '',
    'Set LV24_SEO_BIN=/absolute/path/to/lv24-seo to bypass auto-detection.',
  ].join('\n');
}

function run(list, index) {
  if (index >= list.length) {
    process.stderr.write(hint() + '\n');
    process.exit(127);
  }

  const current = list[index];
  // Backstop: even if a foreign-looking wrapper turns out to be this launcher,
  // never chain more than one fallback hop, including an explicit wrapper.
  const childEnv = { ...process.env };
  if (current.command !== 'uvx') {
    const depth = launcherDepth();
    if (depth >= 1) {
      process.stderr.write(
        'lv24-seo launcher: recursion detected (no usable lv24-seo binary in PATH)\n',
      );
      process.exit(1);
    }
    childEnv.LV24_SEO_LAUNCHER_DEPTH = String(depth + 1);
  }
  const child = spawn(current.command, [...current.prefix, ...args], {
    stdio: 'inherit',
    env: childEnv,
    shell: false,
  });

  let started = false;
  child.on('spawn', () => {
    started = true;
  });
  child.on('error', (error) => {
    if (!started && error && error.code === 'ENOENT') {
      run(list, index + 1);
      return;
    }
    process.stderr.write(`lv24-seo launcher: ${error.message}\n`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) {
      try {
        process.kill(process.pid, signal);
      } catch {
        process.exit(1);
      }
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

if (require.main === module) {
  run(candidates(), 0);
}

module.exports = {
  candidates,
  findSystemBinary,
  launcherDepth,
  looksLikeOwnShim,
  npmVersion,
  uvxPrefix,
  validatedOverride,
};
