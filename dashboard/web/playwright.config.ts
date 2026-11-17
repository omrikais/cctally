import { defineConfig } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { lstatSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

// #923 — a per-run loopback port. Never a fixed one: a stale listener on a
// fixed port (a lost run's dashboard, or anything else) would fail the run, and
// no e2e script may signal a listener it does not own. The runner process
// resolves the port once; each worker re-evaluates this config and must inherit
// it through the environment, as must e2e/serve.sh (Playwright's webServer
// inherits process.env). CCTALLY_E2E_PORT pins a port and is never re-allocated.
function parsePort(value: string, source: string): number {
  const port = /^[1-9][0-9]{3,4}$/.test(value) ? Number(value) : NaN;
  if (!(port >= 1024 && port <= 65535)) {
    throw new Error(`CCTALLY_E2E_PORT must be a decimal port in 1024-65535 (${source} gave '${value}')`);
  }
  return port;
}

// Listens on an OS-assigned loopback port and prints it only once the probe
// socket has closed; a listen error exits non-zero. The bind-then-release
// window before the dashboard binds is a stated limitation (spec 4.1).
const ALLOCATE = `
const server = require('node:net').createServer();
server.on('error', (error) => { console.error(error.message); process.exit(1); });
server.listen(0, '127.0.0.1', () => {
  const { port } = server.address();
  server.close(() => process.stdout.write(String(port)));
});
`;

function resolvePort(): number {
  const pinned = process.env.CCTALLY_E2E_PORT;
  if (pinned !== undefined) return parsePort(pinned, 'the environment');
  if (process.env.TEST_WORKER_INDEX !== undefined) {
    throw new Error('CCTALLY_E2E_PORT is unset in a test worker; the runner process sets it');
  }
  let output: string;
  try {
    output = execFileSync(process.execPath, ['-e', ALLOCATE], { encoding: 'utf8', timeout: 10_000 });
  } catch (error) {
    throw new Error(`CCTALLY_E2E_PORT allocator failed: ${(error as Error).message}`);
  }
  const port = parsePort(output.trim(), 'the allocator');
  process.env.CCTALLY_E2E_PORT = String(port);
  return port;
}

const port = resolvePort();
const origin = `http://127.0.0.1:${port}/`;

const safeEvidence = process.env.CCTALLY_E2E_SAFE_EVIDENCE === '1';
const evidenceDir = process.env.CCTALLY_E2E_EVIDENCE_DIR || '';
if (safeEvidence) {
  if (!isAbsolute(evidenceDir) || !isAbsolute(process.env.CCTALLY_E2E_RUNTIME_DIR || '')) {
    throw new Error('safe e2e evidence requires absolute evidence and runtime directories');
  }
  // The main runner must refuse earlier output before Playwright clears it.
  // Test workers reload this config after that run has created its own output;
  // Playwright sets TEST_WORKER_INDEX before the worker's config deserialization.
  if (process.env.TEST_WORKER_INDEX === undefined) {
    for (const name of ['test-results', 'playwright-report']) {
      if (lstatSync(resolve(evidenceDir, name), { throwIfNoEntry: false })) {
        throw new Error(`safe e2e evidence already exists: ${resolve(evidenceDir, name)}`);
      }
    }
  }
}

// #281 S3 — the conversation-reader real-browser smoke net. Frozen harness
// policy (spec §6): a per-run port, chromium-only, fixed 1440x900 viewport,
// workers 1 (serial — the suite shares ONE fixture server + mutates the live-tail
// file), retries 0 (a flake is a bug that gets an issue — the #283 discipline).
// The `webServer` OWNS its server + fixture state, so reuseExistingServer is
// false unconditionally: an occupied port (a pinned CCTALLY_E2E_PORT) must fail
// loudly rather than silently reuse arbitrary cache/config state (the data dir
// is fixed at process init).
export default defineConfig({
  testDir: 'e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // Never let a stray `.only` pass CI green.
  forbidOnly: !!process.env.CI,
  timeout: 30_000,
  ...(safeEvidence ? { outputDir: resolve(evidenceDir, 'test-results') } : {}),
  // The HTML reporter is what materializes playwright-report/ for the CI upload;
  // `open: 'never'` keeps it from launching a browser locally.
  reporter: [['list'], ['html', {
    open: 'never',
    ...(safeEvidence ? { outputFolder: resolve(evidenceDir, 'playwright-report') } : {}),
  }]],
  use: {
    baseURL: origin,
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: 'bash e2e/serve.sh',
    url: origin,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
