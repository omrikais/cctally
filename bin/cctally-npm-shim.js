#!/usr/bin/env node
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');

if (process.platform === 'win32') {
  console.error('cctally: Windows is not supported. Use macOS or Linux.');
  process.exit(1);
}

const scriptPath = path.join(__dirname, 'cctally');
if (!fs.existsSync(scriptPath)) {
  console.error(`cctally: bundled script not found at ${scriptPath}`);
  process.exit(1);
}

const python = process.env.CCTALLY_PYTHON || 'python3';
const result = spawnSync(python, [scriptPath, ...process.argv.slice(2)], {
  stdio: 'inherit',
});

if (result.error) {
  if (result.error.code === 'ENOENT') {
    console.error(
      `cctally: cannot find ${python}. Install Python 3.13+ ` +
      'or set CCTALLY_PYTHON to its path.'
    );
    process.exit(1);
  }
  console.error(`cctally: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status === null ? 1 : result.status);
