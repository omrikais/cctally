// CacheReportSettings — inline popover anchored to the modal header
// gear icon.
//
// Single user-configurable threshold in v1 (``anomaly_threshold_pp``).
// Save dispatches ``POST /api/settings`` with body
// ``{ cache_report: { anomaly_threshold_pp: N } }``; the server returns
// HTTP 400 on invalid values (per Implementor 1's
// ``_validate_cache_report_settings``) with body
// ``{ error: "...", field: "anomaly_threshold_pp" }``. We surface the
// error message inline under the input. A successful 200 flashes
// ``Saved`` for ~1 s and the popover stays open so the user sees the
// confirmation; the next SSE tick re-renders the modal with the new
// threshold.
//
// Click-outside + ESC both close the popover. ESC stops propagation so
// the modal-level ESC handler doesn't also fire (the popover is
// mutually exclusive with the modal's Escape-to-close per spec §3.10).
//
// Spec 2026-05-21 §3.10 + §6.
import { useState, useEffect, useRef } from 'react';

export interface CacheReportSettingsProps {
  current_threshold_pp: number;
  onClose: () => void;
}

export function CacheReportSettings({
  current_threshold_pp,
  onClose,
}: CacheReportSettingsProps) {
  const [value, setValue] = useState(String(current_threshold_pp));
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const popRef = useRef<HTMLDivElement>(null);

  // Click-outside to close. Bind on mousedown (not click) so the
  // close fires before any embedded onClick handlers run.
  useEffect(() => {
    function onDocClick(e: MouseEvent) {
      if (popRef.current && !popRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [onClose]);

  // ESC closes (capture-phase so the modal-level ESC doesn't also fire).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  async function save() {
    setSaved(false);
    const n = parseInt(value, 10);
    if (!Number.isInteger(n) || n < 1 || n > 100) {
      setErr('Must be an integer between 1 and 100');
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cache_report: { anomaly_threshold_pp: n } }),
      });
      if (r.ok) {
        // Show confirmation; the next sync tick replaces the modal's
        // cr.anomaly_threshold_pp via the snapshot. The popover stays
        // open so the user sees the "Saved" affordance before
        // dismissing.
        setSaved(true);
        setTimeout(() => setSaved(false), 1500);
      } else {
        const body = await r.json().catch(() => ({} as Record<string, unknown>));
        const msg = typeof body.error === 'string' ? body.error : `HTTP ${r.status}`;
        setErr(msg);
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      ref={popRef}
      className="crm-settings-popover"
      role="dialog"
      aria-label="Cache Report settings"
    >
      <h4>Cache Report settings</h4>
      <label htmlFor="cr-threshold">Anomaly threshold (pp)</label>
      <input
        id="cr-threshold"
        type="number"
        inputMode="numeric"
        min={1}
        max={100}
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setErr(null);
          setSaved(false);
        }}
        className={err ? 'invalid' : ''}
      />
      {err && <div className="err">{err}</div>}
      {saved && <div className="saved">Saved · next sync will re-evaluate</div>}
      <div className="actions">
        <button onClick={onClose} type="button">
          Close
        </button>
        <button className="primary" onClick={save} disabled={saving} type="button">
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </div>
  );
}
