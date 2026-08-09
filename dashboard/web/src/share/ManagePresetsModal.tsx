// Spec §6.6 + §11.3 — list saved presets across all panels with
// rename / delete affordances.
//
// Rename is ONE request to `POST /api/share/presets/rename` (#503 S3 §1).
// It used to be a client-side "save under new name, then delete old" pair,
// which rebuilt the record from the four fields this modal happened to hold
// — so it dropped `source`, reset `saved_at`, and silently overwrote any
// preset already holding the target name. The endpoint moves the stored
// record whole under one config writer lock, and answers a collision with
// HTTP 409 `preset_name_conflict`.
//
// #503 S4 §3.1 / #531 item 2 — this dialog now honours the `aria-modal="true"`
// it has always claimed. It used to render as a plain descendant of
// `.share-modal` with no CSS rule of its own, so it computed to
// `position: static; z-index: auto`: an in-flow block at the bottom of the
// share modal that contained no focus and covered nothing. It now renders
// inside a fixed `.share-manage-overlay`, traps Tab with `useModalFocus`, and
// owns Escape at overlay layer 208 rather than at `modal` scope.
//
// ShareModal's `when: () => !manageOpen` guard stays, but it is no longer the
// mechanism: at overlay/208 the ordering is structural — above the 205 preset
// popovers, below the 210 composer, and below an armed confirmation at 220,
// which must keep owning Escape while armed.
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  listPresets, deletePreset, renamePreset, PRESET_NAME_CONFLICT,
  type PresetRecord, type SavedPreset, ShareApiError,
} from './presetsApi';
import type { SharePanelId } from './types';
import { dispatch } from '../store/store';
import { useKeymap } from '../hooks/useKeymap';
import { useModalFocus } from '../hooks/useModalFocus';
import { sharePanelLabel } from './panelLabels';
import { SHARE_PRESETS_TRIGGER_ID } from './PresetDropdown';
import { ModalHeader } from '../modals/ModalHeader';
import { ConfirmAction, useConfirmHost, type ConfirmHost } from './ConfirmAction';

interface Props {
  open: boolean;
  onClose: () => void;
  // Is the share modal still the store's topmost focus layer? Only the
  // topmost surface traps Tab, so ShareModal hands the trap over rather than
  // both surfaces claiming it. Defaults to `true` so a standalone render (and
  // every existing test that does one) still traps.
  shareIsTopmost?: boolean;
}

// Stable id for aria-labelledby — spec §12.4 names the dialog via the
// visible header element rather than an inline aria-label so screen
// readers read what's actually painted.
const MANAGE_PRESETS_TITLE_ID = 'share-manage-presets-title';

// #503 S3 §2 — the focus fallback after a destructive confirm. Confirming a
// delete removes the initiating button and a rename changes the row key, so
// literal restoration is impossible: focus goes to the next row's first
// action, and to the table's Name heading when the confirmed row was the
// last one. The heading takes `tabIndex={-1}` for exactly that reason.
const MANAGE_PRESETS_HEADING_ID = 'share-manage-presets-name-heading';

// `alsoRemoved` names a SECOND row the same operation destroys. An
// overwrite-rename destroys the row holding the target name, and that row can
// be the very next one — in which case the plain "next row's first action"
// answer resolves to a button that is about to leave the DOM, `restore()`
// declines to focus a detached node, and focus lands nowhere at all. So the
// scan walks past every row this operation removes.
function focusAfterRowRemoval(
  panel: string, name: string, alsoRemoved?: string,
): HTMLElement | null {
  const rows = Array.from(
    document.querySelectorAll<HTMLTableRowElement>('.share-manage-table tbody tr'),
  );
  const doomed = new Set([`${panel}/${name}`]);
  if (alsoRemoved != null) doomed.add(`${panel}/${alsoRemoved}`);
  const idx = rows.findIndex((r) => r.dataset.presetKey === `${panel}/${name}`);
  if (idx >= 0) {
    for (const next of rows.slice(idx + 1)) {
      if (next.dataset.presetKey != null && doomed.has(next.dataset.presetKey)) {
        continue;
      }
      const action = next.querySelector<HTMLElement>('.share-manage-actions button');
      if (action) return action;
    }
  }
  return document.getElementById(MANAGE_PRESETS_HEADING_ID);
}

interface Row {
  panel: SharePanelId;
  name: string;
  record: PresetRecord;
}

export function ManagePresetsModal({ open, onClose, shareIsTopmost = true }: Props) {
  const [rows, setRows] = useState<Row[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // ONE confirmation slot for the whole table — see ConfirmAction.tsx for
  // why this cannot be per-row state.
  const confirm = useConfirmHost();

  // #503 S4 §3.1 — was scope:'modal', which fired only because ShareModal
  // gates itself out with `when: () => !manageOpen`. At overlay/208 the
  // ordering is structural: above the 205 preset popovers, below the 210
  // composer, and below an armed confirmation at 220, which still owns Esc
  // while armed.
  const bindings = useMemo(
    () => open
      ? [{ key: 'Escape', scope: 'overlay' as const, layer: 208, action: onClose }]
      : [],
    [open, onClose],
  );
  useKeymap(bindings);

  // Focus containment + restoration. This REPLACES a hand-rolled effect that
  // captured `document.activeElement` when `open` turned true — the "Manage
  // presets…" item inside the preset dropdown, which closes as this dialog
  // opens. The captured node was therefore always detached and the code always
  // took its blur-and-focus-body fallback. `useModalFocus` resolves the trigger
  // by id at restore time, so focus returns to the durable presets trigger.
  //
  // Do NOT mark `.share-modal` inert or aria-hidden while this is open:
  // `useModalFocus`'s getFocusable rejects any element with such an ancestor,
  // and this card is a descendant, so doing that would empty its own focusable
  // set and disable the very trap being added.
  const cardRef = useRef<HTMLDivElement>(null);
  useModalFocus(cardRef, {
    active: open,
    // Only the topmost surface traps. ShareModal yields while we are open.
    trapEnabled: shareIsTopmost && open,
    triggerId: SHARE_PRESETS_TRIGGER_ID,
    initialFocus: 'heading',
  });

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    (async () => {
      try {
        const resp = await listPresets();
        if (cancelled) return;
        const flat: Row[] = [];
        for (const [panel, bucket] of Object.entries(resp.presets)) {
          for (const [name, record] of Object.entries(bucket)) {
            flat.push({ panel: panel as SharePanelId, name, record });
          }
        }
        flat.sort((a, b) =>
          a.panel === b.panel ? a.name.localeCompare(b.name) : a.panel.localeCompare(b.panel),
        );
        setRows(flat);
      } catch (err: unknown) {
        if (cancelled) return;
        const msg =
          err instanceof ShareApiError
            ? err.message ?? `HTTP ${err.status}`
            : (err as Error).message;
        setError(msg ?? 'Failed to load presets');
      }
    })();
    return () => { cancelled = true; };
  }, [open]);

  async function handleDelete(row: Row) {
    setBusy(true);
    setError(null);
    // Resolved BEFORE the row leaves the DOM, because afterwards there is no
    // row to count from. The element itself is CAPTURED here and the closure
    // handed to `confirm.close` returns that same node — it is not looked up
    // again. Surviving rows keep their React key, so their DOM nodes survive
    // the re-render; `restore()` declines a detached node either way.
    const focusTarget = focusAfterRowRemoval(row.panel, row.name);
    try {
      await deletePreset(row.panel, row.name);
    } catch (err: unknown) {
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setError(msg ?? 'Delete failed');
      setBusy(false);
      return;
    }
    setRows((curr) =>
      curr.filter((r) => !(r.panel === row.panel && r.name === row.name)),
    );
    // `setBusy(false)` MUST precede `confirm.close`. Every row action renders
    // `disabled={busy}`, and React applies queued updates in call order, so
    // clearing the flag first is what puts the re-enable in the same commit
    // the deferred focus restore reads. This used to sit in a `finally` that
    // ran after the close: the restore then ran against a still-disabled
    // button, the call was absorbed, and focus stayed on <body>.
    setBusy(false);
    confirm.close(() => focusTarget);
    dispatch({ type: 'SHOW_STATUS_TOAST', text: `Deleted preset "${row.name}"` });
  }

  // Returns the outcome rather than swallowing it, because a collision is
  // not an error the user should read as a failure — it is the moment the
  // UI asks whether to replace. The 409 `code` is the discriminator, not
  // the client's own name-list preflight, which can go stale.
  async function handleRename(
    row: Row, nextName: string, overwrite = false,
  ): Promise<'ok' | 'conflict' | 'error'> {
    setBusy(true);
    setError(null);
    let saved: SavedPreset;
    try {
      saved = await renamePreset({
        panel: row.panel,
        from_name: row.name,
        to_name: nextName,
        ...(overwrite ? { overwrite: true } : {}),
      });
    } catch (err: unknown) {
      // Cleared before returning, for the same reason handleDelete clears it
      // before `confirm.close`: the overwrite confirmation closes the moment
      // this promise settles, and its focus target renders `disabled={busy}`.
      setBusy(false);
      if (err instanceof ShareApiError
          && err.code === PRESET_NAME_CONFLICT) {
        return 'conflict';
      }
      const msg =
        err instanceof ShareApiError
          ? err.message ?? `HTTP ${err.status}`
          : (err as Error).message;
      setError(msg ?? 'Rename failed');
      return 'error';
    }
    setRows((curr) => {
      // An overwrite consumed whatever held the target name.
      const survivors = overwrite
        ? curr.filter((r) => !(r.panel === row.panel && r.name === nextName))
        : curr;
      return survivors.map((r) =>
        (r.panel === row.panel && r.name === row.name)
          // The server answers with the MOVED record, so the row shows
          // what is stored. Carrying the stale `record` forward is what
          // made the "Saved at" cell disagree with the server.
          ? {
            ...r,
            name: nextName,
            record: {
              template_id: saved.template_id,
              options: saved.options,
              saved_at: saved.saved_at,
              ...(saved.source ? { source: saved.source } : {}),
            },
          }
          : r,
      );
    });
    // Before the caller's `.then` runs, so the commit that closes the
    // overwrite confirmation is also the commit that re-enables the row
    // actions the restore is about to focus.
    setBusy(false);
    dispatch({ type: 'SHOW_STATUS_TOAST', text: `Renamed to "${nextName}"` });
    return 'ok';
  }

  if (!open) return null;
  return (
    // The backdrop blocks pointer interaction with the share modal beneath and
    // deliberately gains NO click-to-dismiss: that would be a new affordance in
    // a corrections-only session.
    <div className="share-manage-overlay">
      <div
        className="share-manage-modal"
        ref={cardRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={MANAGE_PRESETS_TITLE_ID}
        onClick={(e) => e.stopPropagation()}
      >
        <ModalHeader
          title="Manage presets"
          titleId={MANAGE_PRESETS_TITLE_ID}
          className="share-manage-header"
          onClose={onClose}
          closeClassName="share-manage-close"
        />
        {error ? (
          <div className="share-manage-error" role="alert">{error}</div>
        ) : null}
        {rows.length === 0 && !error ? (
          <p className="share-manage-empty">No saved presets yet.</p>
        ) : null}
        {rows.length > 0 ? (
          <table className="share-manage-table">
            <thead>
              <tr>
                <th>Panel</th>
                <th id={MANAGE_PRESETS_HEADING_ID} tabIndex={-1}>Name</th>
                <th>Saved at</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <ManagePresetRow
                  key={`${row.panel}/${row.name}`}
                  row={row}
                  busy={busy}
                  confirm={confirm}
                  onDelete={() => void handleDelete(row)}
                  onRename={(next, overwrite) => handleRename(row, next, overwrite)}
                />
              ))}
            </tbody>
          </table>
        ) : null}
      </div>
    </div>
  );
}

function ManagePresetRow({ row, busy, confirm, onDelete, onRename }: {
  row: Row;
  busy: boolean;
  confirm: ConfirmHost;
  onDelete: () => void;
  onRename: (next: string, overwrite?: boolean) =>
    Promise<'ok' | 'conflict' | 'error'>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.name);
  // The name the server refused because something already holds it. Kept so
  // the confirmation can re-issue the SAME rename with `overwrite`.
  const [pendingName, setPendingName] = useState<string | null>(null);
  const rowKey = `${row.panel}/${row.name}`;
  const deleteKey = `delete:${rowKey}`;
  const renameKey = `rename:${rowKey}`;

  const commitRename = () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === row.name) {
      setDraft(row.name);
      setEditing(false);
      return;
    }
    setEditing(false);
    void onRename(trimmed).then((outcome) => {
      // The 409 `code` is the discriminator, not a client-side name-list
      // preflight, which can go stale between its GET and the write.
      if (outcome === 'conflict') {
        setPendingName(trimmed);
        confirm.arm(renameKey);
      }
    });
  };

  return (
    <tr data-preset-key={rowKey}>
      <td>{sharePanelLabel(row.panel)}</td>
      <td>
        {editing ? (
          <input
            className="share-manage-name-input"
            autoFocus
            value={draft}
            disabled={busy}
            maxLength={64}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commitRename();
              } else if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                setDraft(row.name);
                setEditing(false);
              }
            }}
          />
        ) : (
          <span>{row.name}</span>
        )}
      </td>
      <td>{row.record.saved_at}</td>
      <td className="share-manage-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (editing) {
              setDraft(row.name);
              setEditing(false);
            } else {
              setEditing(true);
            }
          }}
        >
          {editing ? 'Cancel' : 'Rename'}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => confirm.arm(deleteKey)}
        >
          Delete
        </button>
        <ConfirmAction
          id={deleteKey}
          host={confirm}
          prompt={`Delete "${row.name}"?`}
          confirmLabel="Delete"
          onConfirm={onDelete}
        />
        <ConfirmAction
          id={renameKey}
          host={confirm}
          prompt={`A preset named "${pendingName ?? ''}" already exists`}
          confirmLabel="Replace it"
          onConfirm={() => {
            const target = pendingName;
            if (target == null) return;
            // The overwrite destroys the row holding `target` as well as
            // moving this one, so that row is excluded from the scan.
            const focusTarget = focusAfterRowRemoval(
              row.panel, row.name, target,
            );
            void onRename(target, true).then((outcome) => {
              if (outcome === 'ok') {
                setPendingName(null);
                confirm.close(() => focusTarget);
              }
            });
          }}
        />
      </td>
    </tr>
  );
}
