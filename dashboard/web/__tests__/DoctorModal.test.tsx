import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DoctorModal } from '../src/components/DoctorModal';
import { dispatch, getState, _resetForTests } from '../src/store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../src/store/keymap';

const SAMPLE_REPORT = {
  schema_version: 1,
  generated_at: '2026-05-13T10:00:00Z',
  cctally_version: '1.7.0',
  overall: { severity: 'warn', counts: { ok: 4, warn: 2, fail: 0 } },
  categories: [
    {
      id: 'install',
      title: 'Install',
      severity: 'warn',
      checks: [
        {
          id: 'install.path',
          title: 'PATH',
          severity: 'warn',
          summary: '~/.local/bin not on $PATH',
          remediation: 'Append the export line to your shell rc',
          details: { homePath: '/Users/x' },
        },
      ],
    },
    {
      id: 'hooks',
      title: 'Hooks',
      severity: 'ok',
      checks: [
        {
          id: 'hooks.installed',
          title: 'Hook entries installed',
          severity: 'ok',
          summary: 'PostToolBatch, Stop, SubagentStop',
          details: {},
        },
      ],
    },
  ],
};

describe('<DoctorModal />', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(SAMPLE_REPORT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  });
  afterEach(() => {
    uninstallGlobalKeydown();
    vi.restoreAllMocks();
  });

  it('renders nothing when doctorModalOpen is false', () => {
    const { container } = render(<DoctorModal />);
    expect(container.firstChild).toBeNull();
  });

  it('fetches the report on open and renders categories', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('Install')).toBeInTheDocument());
    expect(screen.getByText('Hooks')).toBeInTheDocument();
    expect(screen.getByText(/4 OK · 2 WARN · 0 FAIL/)).toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/doctor');
  });

  it('auto-expands non-OK categories and shows the check + remediation', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('PATH')).toBeInTheDocument());
    expect(screen.getByText(/~\/.local\/bin not on \$PATH/)).toBeInTheDocument();
    expect(screen.getByText(/Append the export line/)).toBeInTheDocument();
  });

  it('keeps OK categories collapsed by default (toggle to expand)', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('Hooks')).toBeInTheDocument());
    // Hooks category is OK — the inner check title shouldn't be visible.
    expect(screen.queryByText('Hook entries installed')).not.toBeInTheDocument();
    // Click the Hooks header to expand.
    fireEvent.click(screen.getByText('Hooks'));
    expect(screen.getByText('Hook entries installed')).toBeInTheDocument();
  });

  it('Esc closes the modal', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('Install')).toBeInTheDocument());
    expect(getState().doctorModalOpen).toBe(true);
    const ev = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    document.dispatchEvent(ev);
    expect(getState().doctorModalOpen).toBe(false);
  });

  it('refresh button re-fetches', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('Install')).toBeInTheDocument());
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByLabelText('Refresh doctor report'));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
  });

  it('toggles the per-check details disclosure', async () => {
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText('PATH')).toBeInTheDocument());
    // Initially details hidden.
    expect(screen.queryByText(/homePath/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText(/details/));
    expect(screen.getByText(/homePath/)).toBeInTheDocument();
  });

  it('surfaces an error when the fetch fails', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValueOnce(
      new Response('boom', { status: 500 }),
    );
    render(<DoctorModal />);
    dispatch({ type: 'OPEN_DOCTOR_MODAL' });
    await waitFor(() => expect(screen.getByText(/Error loading report/)).toBeInTheDocument());
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument();
  });
});
