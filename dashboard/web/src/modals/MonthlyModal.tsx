import { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { PeriodDetailCard } from './PeriodDetailCard';
import { PeriodTable } from './PeriodTable';
import { keyOf } from './periodNav';
import { ShareIcon } from '../components/ShareIcon';
import { useSnapshot } from '../hooks/useSnapshot';
import { registerKeymap } from '../store/keymap';
import { dispatch, getState } from '../store/store';
import { openShareModal } from '../store/shareSlice';

export function MonthlyModal() {
  const env = useSnapshot();
  const rows = env?.monthly?.rows ?? [];
  const [selectedIndex, setSelectedIndex] = useState(0);
  const rowCount = rows.length;

  useEffect(() => {
    return registerKeymap([
      {
        key: 'ArrowDown',
        scope: 'modal',
        when: () => getState().openModal === 'monthly',
        action: () => setSelectedIndex((i) => Math.min(i + 1, Math.max(0, rowCount - 1))),
      },
      {
        key: 'ArrowUp',
        scope: 'modal',
        when: () => getState().openModal === 'monthly',
        action: () => setSelectedIndex((i) => Math.max(i - 1, 0)),
      },
    ]);
  }, [rowCount]);

  const headerExtras = (
    <ShareIcon
      panel="monthly"
      panelLabel="Monthly"
      triggerId="monthly-modal"
      onClick={() => dispatch(openShareModal('monthly', 'monthly-modal'))}
    />
  );

  if (rowCount === 0) {
    return (
      <Modal
        title="Monthly history · last 12"
        accentClass="accent-pink"
        headerExtras={headerExtras}
      >
        <div className="panel-empty">No usage history yet.</div>
      </Modal>
    );
  }

  const row = rows[Math.min(selectedIndex, rowCount - 1)];
  // S8: PeriodTable is now key-based. This modal keeps its index-based
  // state + ↑/↓ keymap; adapt at the boundary (index ↔ key). Removed
  // wholesale in Milestone B when HistoryModal supersedes it.
  const selectedKey = row ? keyOf(row, 'month') : null;
  const onSelectKey = (key: string): void => {
    const i = rows.findIndex((r) => keyOf(r, 'month') === key);
    if (i >= 0) setSelectedIndex(i);
  };

  return (
    <Modal
      title="Monthly history · last 12"
      accentClass="accent-pink"
      headerExtras={headerExtras}
    >
      <PeriodDetailCard row={row} variant="monthly" accentClass="accent-pink" />
      <PeriodTable
        rows={rows}
        variant="monthly"
        accentClass="accent-pink"
        selectedKey={selectedKey}
        onSelect={onSelectKey}
      />
    </Modal>
  );
}
