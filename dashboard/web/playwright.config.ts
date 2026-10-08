import { defineConfig } from '@playwright/test';
import { lstatSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

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
// policy (spec §6): dedicated port 8797, chromium-only, fixed 1440x900 viewport,
// workers 1 (serial — the suite shares ONE fixture server + mutates the live-tail
// file), retries 0 (a flake is a bug that gets an issue — the #283 discipline).
// The `webServer` OWNS its server + fixture state, so reuseExistingServer is
// false unconditionally: an occupied 8797 must fail loudly rather than silently
// reuse arbitrary cache/config state (the data dir is fixed at process init).
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
    baseURL: 'http://127.0.0.1:8797/',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: 'bash e2e/serve.sh',
    url: 'http://127.0.0.1:8797/',
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
