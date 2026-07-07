#!/usr/bin/env node
'use strict';

// Print a "next step: run cctally setup" hint after `npm install -g cctally`.
// Mirrors what brew shows via Formula#caveats. Never auto-executes setup —
// `cctally setup` is interactive (legacy-hook migration prompt) and writes
// outside this package's surface (~/.claude/settings.json).

if (process.env.npm_config_global !== 'true') {
  // Local install (project node_modules) — stay silent.
  process.exit(0);
}

if (process.env.CCTALLY_NPM_POSTINSTALL_QUIET === '1') {
  // Escape hatch for CI / fixtures.
  process.exit(0);
}

// Best-effort symlink self-heal on upgrade (issue #114): additively
// create ~/.local/bin/ symlinks for any new cctally-* subcommands so an
// upgrade doesn't strand them until the user re-runs `cctally setup`.
// MUST NOT fail the npm install — swallow every error, ignore exit code.
try {
  const { spawnSync } = require('child_process');
  const path = require('path');
  const python = process.env.CCTALLY_PYTHON || 'python3';
  const scriptPath = path.join(__dirname, 'cctally');
  spawnSync(python, [scriptPath, 'repair-symlinks'], { stdio: 'inherit' });
} catch (_) {
  // best-effort only
}

process.stdout.write(
  '\ncctally installed.\n' +
  '\nTo finish setup, run:\n' +
  '  cctally setup\n' +
  '\nThis installs additive Claude Code hooks (~/.claude/settings.json)\n' +
  'and bootstraps the local SQLite cache (~/.local/share/cctally/).\n' +
  '\ncctally counts anonymous active installs (a rotating monthly token +\n' +
  'version + OS family; no identity, paths, or usage data). Opt out with\n' +
  '`cctally telemetry off`, CCTALLY_DISABLE_TELEMETRY=1, or DO_NOT_TRACK=1.\n' +
  'How it works: https://github.com/omrikais/cctally/blob/main/docs/telemetry.md\n' +
  '\nDetails: https://github.com/omrikais/cctally#installation\n\n'
);
