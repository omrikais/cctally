import { writeFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';
import { loadManifest, openConversation, settleScroller, READER_BODY } from './utils';

const manifest = loadManifest();
const cycles = Math.max(12, Number(process.env.CCTALLY_BROWSER_SOAK_CYCLES || 30));

interface BrowserSample {
  cycle: number;
  heapBytes: number;
  nodes: number;
  documents: number;
  listeners: number;
  interactionMs: number;
  subscriberSettleMs: number;
  serverSubscribers: number;
  serverQueuedDeliveries: number;
  serverThreads: number;
}

function slope(values: number[]): number {
  if (values.length < 2) return 0;
  const xMean = (values.length - 1) / 2;
  const yMean = values.reduce((total, value) => total + value, 0) / values.length;
  let numerator = 0;
  let denominator = 0;
  values.forEach((value, index) => {
    numerator += (index - xMean) * (value - yMean);
    denominator += (index - xMean) ** 2;
  });
  return denominator === 0 ? 0 : numerator / denominator;
}

function median(values: number[]): number {
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 0
    ? (ordered[middle - 1] + ordered[middle]) / 2
    : ordered[middle];
}

function documentPhaseMatchedDelta(
  early: BrowserSample[],
  late: BrowserSample[],
  value: (sample: BrowserSample) => number,
): number {
  const earlyDocuments = new Set(early.map((sample) => sample.documents));
  const sharedDocuments = [...new Set(late.map((sample) => sample.documents))]
    .filter((documents) => earlyDocuments.has(documents));
  expect(sharedDocuments.length, 'plateau windows must share a document phase')
    .toBeGreaterThan(0);
  return Math.max(...sharedDocuments.map((documents) => (
    median(late.filter((sample) => sample.documents === documents).map(value))
    - median(early.filter((sample) => sample.documents === documents).map(value))
  )));
}

test('dashboard browser memory, DOM, interaction, and SSE ownership plateau', async ({ page, context }, testInfo) => {
  test.setTimeout(Math.max(60_000, cycles * 12_000));
  const cdp = await context.newCDPSession(page);
  await cdp.send('Performance.enable');
  await page.goto('/');
  await expect(page.locator('#main-content .panel-host').first()).toBeVisible();

  const samples: BrowserSample[] = [];
  for (let index = 0; index < cycles; index += 1) {
    const session = index % 2 === 0
      ? manifest.long_session_id
      : manifest.single_page_session_id;
    const started = Date.now();
    await openConversation(page, session);
    await expect(page.locator(READER_BODY)).toBeVisible();
    await settleScroller(page);
    await page.locator(READER_BODY).click({ position: { x: 5, y: 5 } });
    await page.keyboard.press('Escape');
    await expect(page.locator('.conv-reader--empty')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.locator('#main-content .panel-host').first()).toBeVisible();
    if ((index + 1) % 3 === 0) {
      await page.reload();
      await expect(page.locator('#main-content .panel-host').first()).toBeVisible();
    }
    const interactionMs = Date.now() - started;

    // A browser reload closes its old EventSource immediately, but the server
    // can observe that only when the next publish writes to the dead socket.
    // Require the subscriber set to return to its steady-state owner count
    // within one bounded publish window; a genuine connection leak never does.
    const settleStarted = Date.now();
    await expect.poll(async () => {
      const response = await page.request.get('/api/debug/backend');
      const payload = await response.json();
      return Number(payload.memory.owners.sseDelivery.subscriberCount);
    }, { timeout: 12_000, intervals: [100, 250, 500, 1000] }).toBe(1);
    const subscriberSettleMs = Date.now() - settleStarted;

    await cdp.send('HeapProfiler.collectGarbage');
    const heap = await cdp.send('Runtime.getHeapUsage');
    const dom = await cdp.send('Memory.getDOMCounters');
    const debugResponse = await page.request.get('/api/debug/backend');
    expect(debugResponse.status()).toBe(200);
    const debug = await debugResponse.json();
    const sse = debug.memory.owners.sseDelivery;
    samples.push({
      cycle: index + 1,
      heapBytes: Number(heap.usedSize),
      nodes: Number(dom.nodes),
      documents: Number(dom.documents),
      listeners: Number(dom.jsEventListeners),
      interactionMs,
      subscriberSettleMs,
      serverSubscribers: Number(sse.subscriberCount),
      serverQueuedDeliveries: Number(sse.queuedDeliveryCount),
      serverThreads: Number(debug.memory.threadCount),
    });
  }

  const warm = samples.slice(Math.floor(samples.length / 2));
  const heapValues = warm.map((sample) => sample.heapBytes);
  const nodeValues = warm.map((sample) => sample.nodes);
  const documentValues = warm.map((sample) => sample.documents);
  const listenerValues = warm.map((sample) => sample.listeners);
  const plateauSplit = Math.floor(warm.length / 2);
  const earlyPlateau = warm.slice(0, plateauSplit);
  const latePlateau = warm.slice(plateauSplit);
  const plateauDelta = {
    // Reloads temporarily retain old documents until the next publish. Compare
    // heap and node populations only at equal document counts; document growth
    // itself remains an independent gate below. Otherwise a harmless phase
    // imbalance between the 7-sample and 8-sample windows aliases one document
    // (213 fixture nodes on hosted CI) into a fake node leak.
    heapBytes: documentPhaseMatchedDelta(
      earlyPlateau, latePlateau, (sample) => sample.heapBytes),
    nodes: documentPhaseMatchedDelta(
      earlyPlateau, latePlateau, (sample) => sample.nodes),
    documents: median(latePlateau.map((sample) => sample.documents))
      - median(earlyPlateau.map((sample) => sample.documents)),
    listeners: median(latePlateau.map((sample) => sample.listeners))
      - median(earlyPlateau.map((sample) => sample.listeners)),
    serverThreads: median(latePlateau.map((sample) => sample.serverThreads))
      - median(earlyPlateau.map((sample) => sample.serverThreads)),
  };
  const receipt = {
    schemaVersion: 1,
    cycles,
    ceilings: {
      heapPlateauDeltaBytes: 1024 * 1024,
      nodePlateauDelta: 64,
      documentPlateauDelta: 1,
      listenerPlateauDelta: 4,
      serverThreadPlateauDelta: 2,
      interactionMs: 8000,
      subscriberSettleMs: 12_000,
      subscribers: 1,
      queuedDeliveries: 1,
      serverThreads: 64,
    },
    slopes: {
      heapBytesPerCycle: slope(heapValues),
      nodesPerCycle: slope(nodeValues),
      documentsPerCycle: slope(documentValues),
      listenersPerCycle: slope(listenerValues),
    },
    plateauDelta,
    samples,
  };
  console.log(`dashboard-browser-memory-soak ${JSON.stringify(receipt)}`);
  await testInfo.attach('dashboard-browser-memory-soak.json', {
    body: Buffer.from(JSON.stringify(receipt, null, 2)),
    contentType: 'application/json',
  });
  const output = process.env.CCTALLY_BROWSER_SOAK_RECEIPT;
  if (output) writeFileSync(output, `${JSON.stringify(receipt, null, 2)}\n`);

  expect(receipt.plateauDelta.heapBytes).toBeLessThanOrEqual(
    receipt.ceilings.heapPlateauDeltaBytes);
  expect(receipt.plateauDelta.nodes).toBeLessThanOrEqual(
    receipt.ceilings.nodePlateauDelta);
  expect(receipt.plateauDelta.documents).toBeLessThanOrEqual(
    receipt.ceilings.documentPlateauDelta);
  expect(receipt.plateauDelta.listeners).toBeLessThanOrEqual(
    receipt.ceilings.listenerPlateauDelta);
  expect(receipt.plateauDelta.serverThreads).toBeLessThanOrEqual(
    receipt.ceilings.serverThreadPlateauDelta);
  expect(Math.max(...samples.map((sample) => sample.interactionMs))).toBeLessThanOrEqual(
    receipt.ceilings.interactionMs);
  expect(Math.max(...samples.map((sample) => sample.subscriberSettleMs))).toBeLessThanOrEqual(
    receipt.ceilings.subscriberSettleMs);
  expect(Math.max(...samples.map((sample) => sample.serverSubscribers))).toBeLessThanOrEqual(
    receipt.ceilings.subscribers);
  expect(Math.min(...samples.map((sample) => sample.serverSubscribers))).toBe(1);
  expect(Math.max(...samples.map((sample) => sample.serverQueuedDeliveries))).toBeLessThanOrEqual(
    receipt.ceilings.queuedDeliveries);
  expect(Math.max(...samples.map((sample) => sample.serverThreads))).toBeLessThanOrEqual(
    receipt.ceilings.serverThreads);
});
