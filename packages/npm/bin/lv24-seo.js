#!/usr/bin/env node
/**
 * Thin launcher for the Python package `lv24-seo`.
 *
 * `npx @lv24/seo …` is a convenience entry point, not a second implementation: it forwards every
 * argument to `uvx --from lv24-seo lv24-seo …` (or to an already installed `lv24-seo` binary).
 * Exit code, stdout and stderr are passed through unchanged so agents can rely on `--json` and the
 * documented exit codes.
 */
'use strict';

const { spawn } = require('node:child_process');

const PACKAGE = 'lv24-seo';
const args = process.argv.slice(2);

function candidates() {
  const override = process.env.LV24_SEO_BIN;
  if (override) {
    return [{ command: override, prefix: [] }];
  }
  return [
    { command: 'uvx', prefix: ['--from', PACKAGE, PACKAGE] },
    { command: PACKAGE, prefix: [] },
  ];
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
  const child = spawn(current.command, [...current.prefix, ...args], {
    stdio: 'inherit',
    env: process.env,
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
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

run(candidates(), 0);
