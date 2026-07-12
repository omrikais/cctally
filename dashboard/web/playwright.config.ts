import { defineConfig } from '@playwright/test';

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
  // The HTML reporter is what materializes playwright-report/ for the CI upload;
  // `open: 'never'` keeps it from launching a browser locally.
  reporter: [['list'], ['html', { open: 'never' }]],
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
