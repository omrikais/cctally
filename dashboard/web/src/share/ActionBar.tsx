// Action buttons + format radio for the share modal (spec §6.2 footer,
// §6.5 actions table, plan §M1.15).
//
// Three M1-functional actions:
//   Copy     — MD only.   navigator.clipboard.writeText(body) + toast
//   Download — all formats. Blob → anchor.click filename includes
//              `cctally-<panel>-<utcdate>.<ext>`.
//   Open     — HTML / SVG only. window.open(URL.createObjectURL(blob))
//
// M2/M3/M4 stubs render as disabled buttons with explanatory tooltips so
// the affordance is discoverable but the user gets immediate feedback
// that the feature is not yet shipped:
//   PNG          — disabled, "Coming in M4" (SVG only)
//   Print → PDF  — disabled, "Coming in M4" (HTML only)
//   + Basket     — disabled, "Coming in M3"
//   Save preset… — disabled, "Coming in M2"
//
// The format radio also lives here (the spec puts it ABOVE the action
// buttons in the §6.2 ASCII diagram). It calls onOptionsChange with the
// new `format` value so the parent threads the change down to Knobs /
// PreviewPane.
//
// Anon-on-export contract (spec §6.3, §6.5): preview always reveals;
// each export action re-fetches the body with `reveal_projects` set per
// `!options.reveal_projects ? false : true` (i.e. honor the checkbox).
// Because the preview already has a reveal=true copy in the iframe,
// every export does a SEPARATE fetch — never re-use the preview body.
import { useEffect, useRef, useState } from 'react';
import { renderShare, ShareApiError } from './api';
import type { ShareFormat, ShareOptions, SharePanelId } from './types';
import { shareFormatExt } from './panelLabels';
import { dispatch } from '../store/store';
import { SavePresetPopover } from './SavePresetPopover';

interface Props {
  panel: SharePanelId;
  templateId: string | null;
  options: ShareOptions;
  onOptionsChange: (next: ShareOptions) => void;
}

// `cctally-<panel>-<utcdate>.<ext>` per spec §6.5. UTC date format
// matches `_lib_share.py` filename rule (YYYYMMDD).
function shareFilename(panel: SharePanelId, format: ShareFormat): string {
  const utc = new Date().toISOString().slice(0, 10).replaceAll('-', '');
  return `cctally-${panel}-${utc}.${shareFormatExt(format)}`;
}

// MIME for the Blob — match the kernel's `content_type` for HTML/SVG;
// for MD fall back to text/markdown.
function mimeFor(format: ShareFormat): string {
  switch (format) {
    case 'md':
      return 'text/markdown;charset=utf-8';
    case 'html':
      return 'text/html;charset=utf-8';
    case 'svg':
      return 'image/svg+xml;charset=utf-8';
  }
}

// Fire a download by synthesizing an <a> with `download` attribute,
// clicking it, then revoking the object URL on the next tick. Done in
// one place so the three actions share the same DOM dance.
function triggerDownload(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke after a microtask so the click has time to settle.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function ActionBar({ panel, templateId, options, onOptionsChange }: Props) {
  const [busy, setBusy] = useState<null | 'copy' | 'download' | 'open'>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // M2 — "Save preset…" inline popover state. Anchored to the trigger
  // button via a wrapping <div> so click-outside can close it.
  const [savingOpen, setSavingOpen] = useState(false);
  const saveTriggerRef = useRef<HTMLDivElement | null>(null);

  // Click-outside-the-anchor dismisses the popover. mousedown so the
  // press registers before the trigger's click re-opens it.
  useEffect(() => {
    if (!savingOpen) return;
    function handler(e: MouseEvent) {
      const root = saveTriggerRef.current;
      if (!root) return;
      if (!root.contains(e.target as Node)) {
        setSavingOpen(false);
      }
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [savingOpen]);

  // Clear stale error banner when the user pivots to a different format
  // or template — otherwise the prior "Copy failed: …" lingers while the
  // user is exploring a different export path.
  useEffect(() => {
    setActionError(null);
  }, [options.format, templateId]);

  const disabledNoTemplate = templateId == null || busy != null;

  const showToast = (text: string) =>
    dispatch({ type: 'SHOW_STATUS_TOAST', text });

  // Single chokepoint for the three working actions. Renders + uses the
  // result. `reveal_projects` is set from the user's checkbox; the
  // preview is a separate fetch and always reveals.
  const fetchForExport = async (): Promise<{ body: string; format: ShareFormat }> => {
    if (!templateId) throw new Error('no template selected');
    const resp = await renderShare({
      panel,
      template_id: templateId,
      // Send the canonical recipe straight through. The "Anon on export"
      // checkbox already flipped reveal_projects in the parent's
      // options state via Knobs.
      options,
    });
    return { body: resp.body, format: options.format };
  };

  const handleCopy = async () => {
    if (disabledNoTemplate) return;
    setBusy('copy');
    setActionError(null);
    try {
      const { body } = await fetchForExport();
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
        throw new Error('Clipboard API unavailable in this browser');
      }
      await navigator.clipboard.writeText(body);
      showToast('Copied');
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setActionError(`Copy failed: ${msg}`);
    } finally {
      setBusy(null);
    }
  };

  const handleDownload = async () => {
    if (disabledNoTemplate) return;
    setBusy('download');
    setActionError(null);
    try {
      const { body, format } = await fetchForExport();
      const blob = new Blob([body], { type: mimeFor(format) });
      triggerDownload(shareFilename(panel, format), blob);
      showToast('Downloaded');
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setActionError(`Download failed: ${msg}`);
    } finally {
      setBusy(null);
    }
  };

  const handleOpen = async () => {
    if (disabledNoTemplate) return;
    setBusy('open');
    setActionError(null);
    try {
      const { body, format } = await fetchForExport();
      const blob = new Blob([body], { type: mimeFor(format) });
      const url = URL.createObjectURL(blob);
      // The new window owns the blob URL for its lifetime; we don't
      // revoke it (the user may want to keep the tab open). Browsers
      // GC blob URLs when the window unloads.
      window.open(url, '_blank', 'noopener,noreferrer');
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setActionError(`Open failed: ${msg}`);
    } finally {
      setBusy(null);
    }
  };

  const canCopy = options.format === 'md' && !disabledNoTemplate;
  const canOpen =
    (options.format === 'html' || options.format === 'svg') && !disabledNoTemplate;
  const canPng = options.format === 'svg'; // M4
  const canPrint = options.format === 'html'; // M4

  return (
    <div className="share-actions">
      <div className="share-format-row" role="radiogroup" aria-label="Export format">
        <span className="share-format-label">Format:</span>
        {(['md', 'html', 'svg'] as const).map((fmt) => (
          <label key={fmt} className="share-format-radio">
            <input
              type="radio"
              name="share-format"
              value={fmt}
              checked={options.format === fmt}
              onChange={() => onOptionsChange({ ...options, format: fmt })}
            />
            <span>{fmt}</span>
          </label>
        ))}
      </div>

      <div className="share-action-row">
        <button
          type="button"
          className="share-action share-action-copy"
          onClick={handleCopy}
          disabled={!canCopy}
          title={
            options.format !== 'md'
              ? 'Copy is available for Markdown only'
              : busy === 'copy'
                ? 'Copying…'
                : 'Copy to clipboard'
          }
        >
          {busy === 'copy' ? 'Copying…' : 'Copy'}
        </button>
        <button
          type="button"
          className="share-action share-action-download"
          onClick={handleDownload}
          disabled={disabledNoTemplate}
          title={busy === 'download' ? 'Downloading…' : 'Download file'}
        >
          {busy === 'download' ? 'Downloading…' : 'Download'}
        </button>
        <button
          type="button"
          className="share-action share-action-open"
          onClick={handleOpen}
          disabled={!canOpen}
          title={
            !(options.format === 'html' || options.format === 'svg')
              ? 'Open is available for HTML/SVG'
              : busy === 'open'
                ? 'Opening…'
                : 'Open in new tab'
          }
        >
          {busy === 'open' ? 'Opening…' : 'Open'}
        </button>
        <button
          type="button"
          className="share-action share-action-disabled"
          disabled
          aria-disabled="true"
          title={
            canPng
              ? 'PNG export — coming in M4'
              : 'PNG export — available for SVG, coming in M4'
          }
        >
          PNG
        </button>
        <button
          type="button"
          className="share-action share-action-disabled"
          disabled
          aria-disabled="true"
          title={
            canPrint
              ? 'Print → PDF — coming in M4'
              : 'Print → PDF — available for HTML, coming in M4'
          }
        >
          Print → PDF
        </button>
        <button
          type="button"
          className="share-action share-action-disabled"
          disabled
          aria-disabled="true"
          title="Add to basket — coming in M3"
        >
          + Basket
        </button>
      </div>
      {/* Save preset lives AFTER the action row in DOM order so the
          natural tab sequence matches spec §12.2 (tiles → knobs →
          format → actions → save preset). It is visually positioned
          right-aligned via CSS `.share-save-preset { margin-left: auto }`
          inside its own row. M2 — live trigger that hoists
          <SavePresetPopover> inline, anchored to the button. */}
      <div className="share-save-preset-row" ref={saveTriggerRef}>
        <button
          type="button"
          className="share-save-preset"
          disabled={templateId == null || busy != null}
          onClick={() => setSavingOpen((v) => !v)}
          aria-haspopup="dialog"
          aria-expanded={savingOpen}
          title={
            templateId == null
              ? 'Pick a template first'
              : 'Save the current recipe as a named preset'
          }
        >
          Save preset…
        </button>
        {savingOpen && templateId ? (
          <SavePresetPopover
            panel={panel}
            templateId={templateId}
            options={options}
            onSaved={() => setSavingOpen(false)}
            onCancel={() => setSavingOpen(false)}
          />
        ) : null}
      </div>
      {actionError ? (
        <div className="share-action-error" role="alert">
          {actionError}
        </div>
      ) : null}
    </div>
  );
}
