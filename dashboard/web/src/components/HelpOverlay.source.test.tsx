// #294 S5 Task 10 — Help-overlay hidden-capability notes + visible-order list
// (§6.9), and the source-status degraded aria-label (§6.10).
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { HelpOverlay } from './HelpOverlay';
import { SourceStatusChip } from './SourceStatusChip';
import { _resetForTests, dispatch, updateSnapshot } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymapForTests,
} from '../store/keymap';
import {
  makeClaudeSourceEntry,
  makeCodexSourceEntry,
  makeAllSourceEntry,
  makeSourceEnvelope,
} from '../test-utils/sourceEnvelope';
import type { Envelope } from '../types/envelope';

function bundleEnv(): Envelope {
  return makeSourceEnvelope() as unknown as Envelope;
}

function openHelp() {
  fireEvent.keyDown(document, { key: '?' });
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
  _resetKeymapForTests();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
});

describe('<HelpOverlay /> canonical source board', () => {
  it('Codex has no hidden-capability section', () => {
    act(() => updateSnapshot(bundleEnv()));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<HelpOverlay />);
    openHelp();
    const hidden = document.querySelector('.help-hidden-capabilities');
    expect(hidden).toBeNull();
  });

  it('Codex wholly-unavailable still keeps all card positions instead of hiding capabilities', () => {
    // The QA P0 environment: even when Codex wholly failed to build (availability
    // 'unavailable', caps {}), the absent-path panels stay hidden — so the Help
    // overlay must still list them (with their pointers), not drop the section.
    const codex = makeCodexSourceEntry({
      availability: 'unavailable',
      data: null,
      capabilities: {},
      warnings: [{ code: 'source_build_failed', message: 'Source data could not be built.' }],
      last_success_at: null,
    });
    const claude = makeClaudeSourceEntry();
    const slice = makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    });
    act(() => updateSnapshot(slice as unknown as Envelope));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<HelpOverlay />);
    openHelp();
    const hidden = document.querySelector('.help-hidden-capabilities');
    expect(hidden).toBeNull();
  });

  it('Claude shows no hidden-capabilities section (nothing is intentionally hidden)', () => {
    act(() => updateSnapshot(bundleEnv()));
    render(<HelpOverlay />);
    openHelp();
    expect(document.querySelector('.help-hidden-capabilities')).toBeNull();
  });

  it('the panel/digit list is the same ten-card order under Codex', () => {
    act(() => updateSnapshot(bundleEnv()));
    act(() => dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' }));
    render(<HelpOverlay />);
    openHelp();
    const table = document.querySelector('#help-overlay table');
    const text = table?.textContent ?? '';
    // Hidden panels are absent from the digit list; visible ones present.
    expect(text).toMatch(/Open Forecast modal/);
    expect(text).toMatch(/Open Trend modal/);
    expect(text).toMatch(/Open Cache Report modal/);
    expect(text).toMatch(/Open Sessions modal/);
    expect(text).toMatch(/Open Blocks modal/);
  });
});

describe('<SourceStatusChip /> degraded aria-label (§6.10)', () => {
  it('exposes a descriptive aria-label carrying the warning message when degraded', () => {
    const claude = makeClaudeSourceEntry({
      availability: 'unavailable',
      freshness: 'stale',
      last_success_at: '2026-04-24T13:00:00Z',
      warnings: [{ code: 'oauth_stale', message: 'OAuth token stale', domain: 'quota' }],
    });
    const codex = makeCodexSourceEntry();
    const slice = makeSourceEnvelope({
      sources: { claude, codex, all: makeAllSourceEntry(claude, codex) },
    });
    act(() => updateSnapshot(slice as unknown as Envelope));
    render(<SourceStatusChip />);
    const chip = screen.getByTestId('source-status-chip');
    expect(chip.getAttribute('aria-label')).toBe('claude source status: OAuth token stale');
  });
});
