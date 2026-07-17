// Share-modal + composer-modal state slice (spec §6.1).
//
// Two top-level UIState slots, separate from the existing scalar
// `openModal: ModalKind | null`. Decoupled so the share modal can
// layer ON TOP of a panel modal (the user opens a panel modal,
// inspects detail, then clicks the share affordance — the share
// modal appears in front without unmounting the panel modal). A
// generic modal stack would have worked too but adds shared-state
// machinery the rest of the UI doesn't need; per Codex P2 review,
// the chosen approach is two named slots.
//
// `shareReducer` is exported as a pure function so the unit tests
// can drive it without booting the master store. The master
// `dispatch` in store.ts forwards the four `OPEN_SHARE` /
// `CLOSE_SHARE` / `OPEN_COMPOSER` / `CLOSE_COMPOSER` cases through
// this reducer and then re-emits via the subscriber set.
import type { SharePanelId } from '../share/types';
import type { DashboardSelection } from '../types/envelope';

export interface ShareModalState {
  panel: SharePanelId;
  // #294 S5 §7 — the source the share flow was captured under, stamped from
  // `activeSource` at OPEN_SHARE. A mid-flow SET_ACTIVE_SOURCE must NOT restamp
  // this, so every render/compose/history/preset request this flow issues
  // carries the captured source, not the live selection. Defaults to 'claude'.
  source: DashboardSelection;
  // Element id captured when the share modal opens; <ShareModalRoot>
  // uses it to re-acquire the trigger element for focus restoration on
  // close (a11y: matches the existing panel-modal focus-restore pattern).
  // String — not an HTMLElement ref — so the slot stays serializable.
  triggerId: string | null;
  // Opaque per-panel params slot. Spec §7.3: the Projects modal share
  // affordance supplies `windowWeeks` so the rendered share artifact
  // reflects the modal's currently-active trend window (1 / 4 / 8 / 12).
  // Other panels omit; existing call sites unaffected. Kept narrow on
  // purpose — when a new panel needs more params, widen the union here.
  params?: { windowWeeks?: 1 | 4 | 8 | 12 };
}

export interface ComposerModalState {
  // Marker shape only — the composer holds its own form state
  // locally (M3.x), so the slot just needs to be "open" or "null".
  open: true;
}

export interface ShareSlice {
  shareModal: ShareModalState | null;
  composerModal: ComposerModalState | null;
}

export const initialShareState: ShareSlice = {
  shareModal: null,
  composerModal: null,
};

export type ShareAction =
  | {
      type: 'OPEN_SHARE';
      panel: SharePanelId;
      triggerId: string | null;
      params?: { windowWeeks?: 1 | 4 | 8 | 12 };
      // #294 S5 §7 — the master store stamps this from `getState().activeSource`
      // when it forwards OPEN_SHARE (the pure action creator omits it). Defaults
      // to 'claude' in the reducer so direct unit dispatches stay valid.
      source?: DashboardSelection;
    }
  | { type: 'CLOSE_SHARE' }
  | { type: 'OPEN_COMPOSER' }
  | { type: 'CLOSE_COMPOSER' };

export function shareReducer(state: ShareSlice, action: ShareAction): ShareSlice {
  switch (action.type) {
    case 'OPEN_SHARE': {
      // Only carry `params` onto the modal state when the action
      // actually supplies one. Spread-assigning `params: undefined`
      // would leave the key in the object and break narrowed
      // discriminated-union checks downstream.
      const source: DashboardSelection = action.source ?? 'claude';
      const modal: ShareModalState = action.params !== undefined
        ? { panel: action.panel, triggerId: action.triggerId, source, params: action.params }
        : { panel: action.panel, triggerId: action.triggerId, source };
      return { ...state, shareModal: modal };
    }
    case 'CLOSE_SHARE':
      return { ...state, shareModal: null };
    case 'OPEN_COMPOSER':
      return { ...state, composerModal: { open: true } };
    case 'CLOSE_COMPOSER':
      return { ...state, composerModal: null };
    default:
      return state;
  }
}

export function openShareModal(
  panel: SharePanelId,
  triggerId: string | null,
  params?: { windowWeeks?: 1 | 4 | 8 | 12 },
): ShareAction {
  return params !== undefined
    ? { type: 'OPEN_SHARE', panel, triggerId, params }
    : { type: 'OPEN_SHARE', panel, triggerId };
}
export function closeShareModal(): ShareAction { return { type: 'CLOSE_SHARE' }; }
export function openComposer(): ShareAction { return { type: 'OPEN_COMPOSER' }; }
export function closeComposer(): ShareAction { return { type: 'CLOSE_COMPOSER' }; }
