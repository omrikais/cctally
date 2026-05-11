// SavePresetPopover — plan §M2.4 contract:
//   - Empty name → inline "Name is required" error, no POST.
//   - Long name → inline length error, no POST.
//   - Name with '/' → inline error, no POST.
//   - Valid name + Save click → POST /api/share/presets and onSaved.
//   - Enter on input triggers submit; Escape triggers onCancel.
//   - Server 4xx → renders the server-provided error message.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SavePresetPopover } from './SavePresetPopover';
import { _resetForTests } from '../store/store';
import type { ShareOptions } from './types';

function defaults(): ShareOptions {
  return {
    format: 'md',
    theme: 'light',
    reveal_projects: false,
    no_branding: false,
    top_n: 5,
    period: { kind: 'current' },
    project_allowlist: null,
    show_chart: true,
    show_table: true,
  };
}

beforeEach(() => {
  _resetForTests();
});

afterEach(() => vi.restoreAllMocks());

describe('<SavePresetPopover>', () => {
  it('rejects empty names without firing a POST', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const onSaved = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={onSaved}
        onCancel={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/name is required/i);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("rejects names containing '/' with inline error", () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={() => {}}
        onCancel={() => {}}
      />,
    );
    const input = screen.getByLabelText(/preset name/i);
    fireEvent.change(input, { target: { value: 'team/monday' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(screen.getByRole('alert')).toHaveTextContent(/cannot contain/i);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('POSTs the preset and calls onSaved on success', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        panel: 'weekly',
        name: 'team-monday',
        template_id: 'weekly-recap',
        options: defaults(),
        saved_at: '2026-05-11T09:00:00Z',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    const onSaved = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={onSaved}
        onCancel={() => {}}
      />,
    );
    const input = screen.getByLabelText(/preset name/i);
    fireEvent.change(input, { target: { value: 'team-monday' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(fetchSpy).toHaveBeenCalledWith('/api/share/presets', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
    }));
    const body = JSON.parse(
      (fetchSpy.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.name).toBe('team-monday');
    expect(body.panel).toBe('weekly');
  });

  it('Enter submits the form', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        panel: 'weekly', name: 'm', template_id: 'weekly-recap',
        options: defaults(), saved_at: '2026-05-11T09:00:00Z',
      }), { status: 200 }),
    );
    const onSaved = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={onSaved}
        onCancel={() => {}}
      />,
    );
    const input = screen.getByLabelText(/preset name/i);
    fireEvent.change(input, { target: { value: 'm' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it('Escape calls onCancel without firing a fetch', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const onCancel = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={() => {}}
        onCancel={onCancel}
      />,
    );
    const input = screen.getByLabelText(/preset name/i);
    fireEvent.change(input, { target: { value: 'm' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('renders server-side error messages', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({
        error: 'name must be 1-64 chars and contain no /',
        field: 'name',
      }), { status: 400, headers: { 'Content-Type': 'application/json' } }),
    );
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        onSaved={() => {}}
        onCancel={() => {}}
      />,
    );
    const input = screen.getByLabelText(/preset name/i);
    fireEvent.change(input, { target: { value: 'ok-name' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/must be 1-64/i);
  });
});
