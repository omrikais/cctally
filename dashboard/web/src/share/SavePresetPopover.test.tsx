// SavePresetPopover — plan §M2.4 contract:
//   - Empty name → inline "Name is required" error, no POST.
//   - Long name → inline length error, no POST.
//   - Name with '/' → inline error, no POST.
//   - Valid name + Save click → POST /api/share/presets and onSaved.
//   - Enter on input triggers submit; Escape triggers onCancel.
//   - Server 4xx → renders the server-provided error message.
import { useRef } from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SavePresetPopover } from './SavePresetPopover';
import { _resetForTests } from '../store/store';
import {
  installGlobalKeydown,
  uninstallGlobalKeydown,
  _resetForTests as _resetKeymap,
} from '../store/keymap';
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
  // The confirmation's Escape binding goes through the document dispatcher,
  // which only exists once `installGlobalKeydown` has run.
  _resetKeymap();
  installGlobalKeydown();
});

afterEach(() => {
  uninstallGlobalKeydown();
  _resetKeymap();
  vi.restoreAllMocks();
});

describe('<SavePresetPopover>', () => {
  it('rejects empty names without firing a POST', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const onSaved = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        existingNames={[]}
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
        existingNames={[]}
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
        existingNames={[]}
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
        existingNames={[]}
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
        existingNames={[]}
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
        existingNames={[]}
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

// ---- #503 S3 §2 / A7 — the save-overwrite confirmation -------------------
//
// The fourth of A7's four destructive sites. The other three are pinned in
// ManagePresetsModal.test.tsx (delete, rename collision) and in
// ComposerModal.test.tsx (`Clear all`); this one had no coverage at all,
// because every test above passes `existingNames={[]}`, which is exactly the
// input that disables the collision path.

const OVERWRITE_PROMPT = '"team-monday" exists — saving replaces it';

function savedResponse(): Response {
  return new Response(JSON.stringify({
    panel: 'weekly',
    name: 'team-monday',
    template_id: 'weekly-recap',
    options: defaults(),
    saved_at: '2026-05-11T09:00:00Z',
  }), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

/** Type a taken name and click Save. Returns the fetch spy. */
function armOverwrite(response?: () => Response) {
  const fetchSpy = response
    ? vi.spyOn(globalThis, 'fetch').mockImplementation(
      (() => Promise.resolve(response())) as typeof fetch,
    )
    : vi.spyOn(globalThis, 'fetch');
  const input = screen.getByLabelText(/preset name/i);
  fireEvent.change(input, { target: { value: 'team-monday' } });
  fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
  return fetchSpy;
}

describe('<SavePresetPopover> overwrite confirmation', () => {
  it('saving onto a taken name asks first and sends nothing', async () => {
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        existingNames={['team-monday']}
        onSaved={() => {}}
        onCancel={() => {}}
      />,
    );
    const fetchSpy = armOverwrite();
    expect(await screen.findByText(OVERWRITE_PROMPT)).toBeInTheDocument();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('sends overwrite: true only after the confirmation is accepted', async () => {
    const onSaved = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        existingNames={['team-monday']}
        onSaved={onSaved}
        onCancel={() => {}}
      />,
    );
    const fetchSpy = armOverwrite(savedResponse);
    await screen.findByText(OVERWRITE_PROMPT);
    expect(fetchSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const body = JSON.parse(
      (fetchSpy.mock.calls[0][1] as RequestInit).body as string,
    );
    expect(body.overwrite).toBe(true);
    expect(body.name).toBe('team-monday');
  });

  it('Cancel on the confirmation saves nothing and keeps the popover', async () => {
    const onSaved = vi.fn();
    const onCancel = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        existingNames={['team-monday']}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    const fetchSpy = armOverwrite(savedResponse);
    await screen.findByText(OVERWRITE_PROMPT);

    // The popover has a Cancel of its own, so the confirmation's is picked
    // by class — the same discriminator ManagePresetsModal.test.tsx uses.
    const confirmCancel = screen.getAllByRole('button', { name: /^cancel$/i })
      .find((b) => b.classList.contains('share-confirm-no'));
    fireEvent.click(confirmCancel as HTMLElement);

    await waitFor(() =>
      expect(screen.queryByText(OVERWRITE_PROMPT)).not.toBeInTheDocument());
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    // Cancelling a confirmation dismisses the confirmation, not the popover.
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/preset name/i)).toBeInTheDocument();
  });

  it('is keyboard-reachable, announced, and Escape cancels it', async () => {
    const onSaved = vi.fn();
    const onCancel = vi.fn();
    render(
      <SavePresetPopover
        panel="weekly"
        templateId="weekly-recap"
        options={defaults()}
        existingNames={['team-monday']}
        onSaved={onSaved}
        onCancel={onCancel}
      />,
    );
    const fetchSpy = armOverwrite(savedResponse);
    await screen.findByText(OVERWRITE_PROMPT);

    const yes = screen.getByRole('button', { name: 'Replace' });
    // Focus moves to Confirm, so the confirmation is reachable and
    // dismissible without a pointer; Enter and Space are native to a button.
    await waitFor(() => expect(document.activeElement).toBe(yes));
    // Announced: the focused button points at a polite live region holding
    // the full prompt, so a screen-reader user hears it on arrival.
    const promptId = yes.getAttribute('aria-describedby');
    expect(promptId).toBeTruthy();
    const prompt = document.getElementById(promptId as string);
    expect(prompt).toHaveAttribute('role', 'status');
    expect(prompt).toHaveTextContent(OVERWRITE_PROMPT);

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByText(OVERWRITE_PROMPT)).not.toBeInTheDocument());
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('moves focus to the Save trigger after the confirm', async () => {
    // Spec §2: "after a save confirm, to the Save trigger." The Confirm
    // button unmounts on close, so without a supplied target focus falls to
    // <body> — which is what this pins against.
    function Harness() {
      const triggerRef = useRef<HTMLButtonElement | null>(null);
      return (
        <>
          <button type="button" ref={triggerRef}>Save preset…</button>
          <SavePresetPopover
            panel="weekly"
            templateId="weekly-recap"
            options={defaults()}
            existingNames={['team-monday']}
            focusAfterConfirm={() => triggerRef.current}
            onSaved={() => {}}
            onCancel={() => {}}
          />
        </>
      );
    }
    render(<Harness />);
    armOverwrite(savedResponse);
    await screen.findByText(OVERWRITE_PROMPT);

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    const trigger = screen.getByRole('button', { name: 'Save preset…' });
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it('arms the same confirmation on a server 409, with no name list', async () => {
    // The list is an optimisation, never the authority: it can go stale
    // between its GET and the write, and this caller has no list at all.
    const onSaved = vi.fn();
    let calls = 0;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(((
      _url: unknown, init?: RequestInit,
    ) => {
      // The popover fetches the name list when `existingNames` is absent.
      if (!init || init.method !== 'POST') {
        return Promise.resolve(new Response(JSON.stringify({ presets: {} }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      }
      calls += 1;
      if (calls === 1) {
        return Promise.resolve(new Response(JSON.stringify({
          code: 'preset_name_conflict',
          error: "a preset named 'team-monday' already exists",
          field: 'name',
        }), { status: 409, headers: { 'Content-Type': 'application/json' } }));
      }
      return Promise.resolve(savedResponse());
    }) as unknown as typeof fetch);

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

    expect(await screen.findByText(OVERWRITE_PROMPT)).toBeInTheDocument();
    // The 409 is a question, not a failure — no error banner.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Replace' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    const retry = fetchSpy.mock.calls
      .map((c) => c[1] as RequestInit)
      .filter((init) => init && init.method === 'POST');
    expect(JSON.parse(retry[1].body as string).overwrite).toBe(true);
  });
});
