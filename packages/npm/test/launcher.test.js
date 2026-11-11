'use strict';

const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { describe, it } = require('node:test');

const BIN = path.join(__dirname, '..', 'bin', 'lv24-seo.js');
const launcher = require('../bin/lv24-seo.js');

function runNode(args, env) {
  return spawnSync(process.execPath, [BIN, ...args], {
    env: { ...process.env, ...env },
    encoding: 'utf8',
    timeout: 10000,
  });
}

describe('launcher unit surface', () => {
  it('pins uvx to the npm package version', () => {
    const prefix = launcher.uvxPrefix().join(' ');
    const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'package.json'), 'utf8'));
    assert.match(prefix, new RegExp(`^--from lv24-seo==${pkg.version.replace(/\./g, '\\.')} lv24-seo$`));
  });

  it('detects its own file and binary files correctly', () => {
    assert.equal(launcher.looksLikeOwnShim(BIN), true);
    const binary = path.join(os.tmpdir(), `lv24-bin-${Date.now()}`);
    fs.writeFileSync(binary, Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x00]));
    try {
      assert.equal(launcher.looksLikeOwnShim(binary), false);
    } finally {
      fs.unlinkSync(binary);
    }
  });

  it('skips a PATH entry that is a copy of the launcher', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lv24-path-'));
    try {
      fs.copyFileSync(BIN, path.join(dir, 'lv24-seo'));
      const probed = launcher.findSystemBinary.call(null);
      void probed;
      const originalPath = process.env.PATH;
      process.env.PATH = dir;
      try {
        assert.equal(launcher.findSystemBinary(), null);
      } finally {
        process.env.PATH = originalPath;
      }
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('launcher processes', () => {
  it('rejects an explicit npm shim instead of bypassing recursion guards', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lv24-override-'));
    try {
      const shim = path.join(dir, process.platform === 'win32' ? 'lv24-seo.cmd' : 'lv24-seo');
      fs.writeFileSync(shim, '#!/bin/sh\n# @lv24/seo/bin/lv24-seo.js\n', { mode: 0o755 });
      const result = runNode(['--version'], { LV24_SEO_BIN: shim });
      assert.equal(result.status, 2);
      assert.match(result.stderr, /recursion/);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
  it('rejects a relative LV24_SEO_BIN with exit 2', () => {
    const result = runNode(['--version'], { LV24_SEO_BIN: 'relative/lv24-seo' });
    assert.equal(result.status, 2);
    assert.match(result.stderr, /LV24_SEO_BIN/);
  });

  it('passes args, stdout and exit codes through LV24_SEO_BIN', () => {
    const result = runNode(['--version'], { LV24_SEO_BIN: process.execPath });
    assert.equal(result.status, 0);
    assert.match(result.stdout, /^v\d+\./m);
  });

  it('exits 127 with a hint when neither uvx nor a binary exists', () => {
    const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'lv24-empty-'));
    try {
      const result = runNode(['--version'], {
        PATH: empty,
        LV24_SEO_BIN: '',
        LV24_SEO_PIN: '0',
      });
      // No uvx on PATH, no fallback binary: must fail fast, never recurse.
      assert.equal(result.status, 127);
      assert.match(result.stderr, /needs uv/);
    } finally {
      fs.rmSync(empty, { recursive: true, force: true });
    }
  });

  it('never recurses when the only binary is its own shim', () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lv24-shim-'));
    try {
      fs.copyFileSync(BIN, path.join(dir, 'lv24-seo'));
      const started = Date.now();
      const result = runNode(['--version'], {
        PATH: dir,
        LV24_SEO_BIN: '',
        LV24_SEO_PIN: '0',
      });
      assert.ok(Date.now() - started < 15000, 'must terminate quickly');
      assert.ok([1, 127].includes(result.status), `status=${result.status}: ${result.stderr}`);
      assert.match(result.stderr, /recursion detected|needs uv/);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('uses an unpinned lookup only when LV24_SEO_PIN=0', () => {
    const original = process.env.LV24_SEO_PIN;
    process.env.LV24_SEO_PIN = '0';
    try {
      assert.deepEqual(launcher.uvxPrefix(), ['--from', 'lv24-seo', 'lv24-seo']);
    } finally {
      if (original === undefined) {
        delete process.env.LV24_SEO_PIN;
      } else {
        process.env.LV24_SEO_PIN = original;
      }
    }
  });
});
