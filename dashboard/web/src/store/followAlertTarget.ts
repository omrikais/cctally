import { dispatch, getState } from './store';
import { selectionShiftFor, type AlertTarget } from '../lib/alertScope';

// #620 S1 D12 — the one place a resolved alert target becomes dispatches.
//
// It lives beside the store rather than in `lib/alertScope.ts` because that
// module is a pure kernel: it reads no clock and performs no I/O, which is
// what lets the scope tests pin its arithmetic without a store. Four surfaces
// follow a target (the alerts modal rows, the toast, the budget block and the
// forecast modal), so the two-dispatch sequence below is written once.
//
// The order matters and is not incidental. `OPEN_MODAL` captures
// `openModalSource: state.activeSource` at reduce time, so a modal bound to
// the wrong provider is what you get if the selection shift lands second.
// Both dispatches are synchronous, so the pair is atomic from the caller's
// point of view.
export function followAlertTarget(target: AlertTarget): void {
  const shift = selectionShiftFor(target, getState().activeSource);
  if (shift != null) dispatch({ type: 'SET_ACTIVE_SOURCE', source: shift });
  dispatch({
    type: 'OPEN_MODAL',
    kind: target.modal,
    ...(target.blockStartAt == null ? {} : { blockStartAt: target.blockStartAt }),
    ...(target.projectKey == null ? {} : { projectKey: target.projectKey }),
  });
}
