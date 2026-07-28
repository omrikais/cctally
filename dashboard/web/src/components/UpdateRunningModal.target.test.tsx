import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { UpdateRunningModal } from './UpdateRunningModal';
import {
  _resetForTests,
  dispatch,
  type UpdateState,
} from '../store/store';

class FakeEventSource {
  static current: FakeEventSource | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  onerror: (() => void) | null = null;

  constructor(public readonly url: string) {
    FakeEventSource.current = this;
  }

  addEventListener(name: string, callback: EventListenerOrEventListenerObject) {
    const listener = callback as (event: MessageEvent) => void;
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  emit(name: string, payload: Record<string, unknown>) {
    const event = new MessageEvent(name, { data: JSON.stringify(payload) });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close() {}
}

const suppress = { skipped_versions: [], remind_after: null };

function seed(version: string): UpdateState {
  return {
    current_version: '1.8.0',
    latest_version: version,
    available: true,
    method: 'npm',
    update_command: `npm install -g cctally@${version}`,
    release_notes_url: null,
    check_status: 'ok',
    checked_at_utc: '2026-07-26T00:00:00Z',
    prerelease_note: null,
    configured_channel: 'beta',
  };
}

beforeEach(() => {
  _resetForTests();
  FakeEventSource.current = null;
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('refreshes the running modal when the worker resolves a fresher beta target', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    state: {
      current_version: '1.8.0',
      latest_version: '1.9.0',
      install: { method: 'npm' },
      check_status: 'ok',
    },
    suppress,
    configured_channel: 'beta',
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })));

  act(() => {
    dispatch({ type: 'SET_UPDATE_STATE', state: seed('1.8.0'), suppress });
    dispatch({ type: 'SET_UPDATE_STATUS', status: 'running' });
    dispatch({ type: 'SET_UPDATE_RUN_ID', runId: 'run-397', startedAt: Date.now() });
  });
  render(<UpdateRunningModal />);

  expect(screen.getByText('npm install -g cctally@1.8.0')).toBeInTheDocument();
  expect(FakeEventSource.current?.url).toBe('/api/update/stream/run-397');

  act(() => {
    FakeEventSource.current?.emit('target', {
      type: 'target',
      version: '1.9.0',
      command: 'npm install -g cctally@1.9.0',
    });
  });

  await waitFor(() => {
    expect(screen.getByText('npm install -g cctally@1.9.0')).toBeInTheDocument();
    expect(screen.getByText(/install continues to 1\.9\.0/)).toBeInTheDocument();
  });
});
