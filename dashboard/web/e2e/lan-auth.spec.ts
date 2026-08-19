import { expect, test } from '@playwright/test';
import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadManifest } from './utils';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '../../..');
const RUNTIME = resolve(HERE, '.runtime');
const CCTALLY = resolve(REPO_ROOT, 'bin/cctally');

function isolatedEnv(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    CCTALLY_DATA_DIR: resolve(RUNTIME, 'scratch/data'),
    CLAUDE_CONFIG_DIR: resolve(RUNTIME, 'scratch/claude'),
    CODEX_HOME: [
      resolve(RUNTIME, 'scratch/codex-main'),
      resolve(RUNTIME, 'scratch/codex-a'),
      resolve(RUNTIME, 'scratch/codex-b'),
    ].join(','),
    CCTALLY_DISABLE_DEV_AUTODETECT: '1',
    CCTALLY_DISABLE_TELEMETRY: '1',
    CCTALLY_AS_OF: '2026-07-14T16:10:00Z',
  };
}

async function freePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        reject(new Error('failed to allocate a TCP port'));
        return;
      }
      server.close((error) => error ? reject(error) : resolvePort(address.port));
    });
  });
}

async function waitForLanBanner(
  child: ChildProcessWithoutNullStreams,
  port: number,
): Promise<{ token: string; url: string }> {
  return new Promise((resolveBanner, reject) => {
    let output = '';
    const timeout = setTimeout(
      () => reject(new Error(`LAN dashboard banner timed out:\n${output}`)),
      20_000,
    );
    const inspect = (chunk: Buffer) => {
      output += chunk.toString('utf8');
      const token = output.match(/dashboard: LAN access token: ([A-Za-z0-9_-]+)/)?.[1];
      const url = output.match(
        new RegExp(`http://localhost:${port}/#token=[^\\s]+`),
      )?.[0];
      if (token && url) {
        clearTimeout(timeout);
        resolveBanner({ token, url });
      }
    };
    child.stdout.on('data', inspect);
    child.stderr.on('data', inspect);
    child.once('error', (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.once('exit', (code) => {
      clearTimeout(timeout);
      reject(new Error(`LAN dashboard exited early (${code}):\n${output}`));
    });
  });
}

async function stop(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([
    new Promise<void>((resolveExit) => child.once('exit', () => resolveExit())),
    new Promise<void>((resolveTimeout) => setTimeout(resolveTimeout, 3_000)),
  ]);
  if (child.exitCode === null) child.kill('SIGKILL');
}

test('wildcard dashboard authenticates Fetch and both SSE streams per run', async ({
  browser,
  page,
  request,
}) => {
  const env = isolatedEnv();
  const manifest = loadManifest();

  // The already-running loopback dashboard remains credential-free.
  expect((await request.get('/api/data')).status()).toBe(200);
  await page.goto('/');
  // The bootstrap Fetch was removed in #583, so the panel grid can mount only
  // after the dashboard stream delivers a snapshot. SharedWorker requests are
  // deliberately not attributed to a Playwright Page or BrowserContext network
  // event; assert the user-visible delivery instead.
  await expect(page.locator('#main-content .panel-host').first()).toBeVisible();

  execFileSync(CCTALLY, [
    'config', 'set', 'dashboard.expose_transcripts', 'true',
  ], { env, stdio: 'ignore' });
  execFileSync(CCTALLY, [
    'config', 'set', 'dashboard.lan_auth', 'true',
  ], { env, stdio: 'ignore' });

  const port = await freePort();
  const child = spawn(CCTALLY, [
    'dashboard', '--port', String(port), '--host', '0.0.0.0',
    '--no-browser', '--no-sync',
  ], { env });

  try {
    const { token, url } = await waitForLanBanner(child, port);
    expect(decodeURIComponent(new URL(url).hash.slice('#token='.length))).toBe(token);

    const apiOrder: string[] = [];
    let releaseAuth!: () => void;
    let authIntercepted!: () => void;
    const authGate = new Promise<void>((resolveGate) => { releaseAuth = resolveGate; });
    const sawAuth = new Promise<void>((resolveAuth) => { authIntercepted = resolveAuth; });
    await page.route(`http://localhost:${port}/api/auth`, async (route) => {
      authIntercepted();
      await authGate;
      await route.continue();
    });
    page.on('request', (request) => {
      const requestUrl = new URL(request.url());
      if (requestUrl.port === String(port) && requestUrl.pathname.startsWith('/api/')) {
        apiOrder.push(`request:${requestUrl.pathname}`);
      }
    });
    page.on('response', (response) => {
      const responseUrl = new URL(response.url());
      if (responseUrl.port === String(port) && responseUrl.pathname === '/api/auth') {
        apiOrder.push(`response:${responseUrl.pathname}:${response.status()}`);
      }
    });
    const navigation = page.goto(url);
    await sawAuth;
    const documentResponse = await navigation;
    expect(apiOrder).toEqual(['request:/api/auth']);
    releaseAuth();
    expect(documentResponse?.url()).not.toContain(token);
    // As above, mounting the authenticated dashboard is the observable proof
    // that its SharedWorker-owned SSE received a snapshot.
    await expect(page.locator('#main-content .panel-host').first()).toBeVisible();
    expect(apiOrder.slice(0, 2)).toEqual([
      'request:/api/auth',
      'response:/api/auth:204',
    ]);

    expect(await page.evaluate(() => location.hash)).toBe('');
    expect(await page.evaluate(() => document.cookie)).not.toContain(token);
    expect(await page.evaluate(async () => (await fetch('/api/data')).status)).toBe(200);
    expect(await page.evaluate(async () => (
      await fetch('/api/data', {
        headers: { Authorization: 'Bearer wrong' },
      })
    ).status)).toBe(401);

    const cookies = await page.context().cookies(`${new URL(url).origin}/api/data`);
    const session = cookies.find((cookie) => cookie.name === 'cctally_dashboard_token');
    expect(session?.value).toBe(token);
    expect(session?.httpOnly).toBe(true);
    expect(session?.sameSite).toBe('Strict');
    expect(session?.path).toBe('/api');

    const conversationStatus = await page.evaluate(async (sessionId) => (
      new Promise<number>((resolveStatus, reject) => {
        const es = new EventSource(
          `/api/conversation/${encodeURIComponent(sessionId)}/events`,
        );
        const timeout = window.setTimeout(() => {
          es.close();
          reject(new Error('conversation EventSource timed out'));
        }, 10_000);
        es.onopen = () => {
          window.clearTimeout(timeout);
          es.close();
          resolveStatus(200);
        };
        es.onerror = () => {
          window.clearTimeout(timeout);
          es.close();
          reject(new Error('conversation EventSource rejected'));
        };
      })
    ), manifest.live_session_id);
    expect(conversationStatus).toBe(200);

    const restartResult = await page.evaluate(async () => {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dashboard: { lan_auth: false } }),
      });
      return { status: response.status, body: await response.json() };
    });
    expect(restartResult.status).toBe(200);
    expect(restartResult.body.restart_required).toEqual(['dashboard.lan_auth']);

    const clean = await browser.newContext();
    try {
      const cleanPage = await clean.newPage();
      await cleanPage.goto(`http://localhost:${port}/`);
      expect(await cleanPage.evaluate(async () => (
        await fetch('/api/data')
      ).status)).toBe(401);
    } finally {
      await clean.close();
    }
  } finally {
    await stop(child);
  }
});
