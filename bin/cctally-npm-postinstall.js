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

process.stdout.write(
  '\ncctally installed.\n' +
  '\nTo finish setup, run:\n' +
  '  cctally setup\n' +
  '\nThis installs additive Claude Code hooks (~/.claude/settings.json)\n' +
  'and bootstraps the local SQLite cache (~/.local/share/cctally/).\n' +
  '\nDetails: https://github.com/omrikais/cctally#installation\n\n'
);
