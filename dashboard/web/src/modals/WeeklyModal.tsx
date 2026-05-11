import { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { PeriodDetailCard } from './PeriodDetailCard';
import { PeriodTable } from './PeriodTable';
import { ShareIcon } from '../components/ShareIcon';
import { useSnapshot } from '../hooks/useSnapshot';
import { registerKeymap } from '../store/keymap';
import { dispatch, getState } from '../store/store';
import { openShareModal } from '../store/shareSlice';

export function WeeklyModal() {
  const env = useSnapshot();
  const rows = env?.weekly?.rows ?? [];
  const [selectedIndex, setSelectedIndex] = useState(0);
  const rowCount = rows.length;

  // Modal-scoped Up/Down navigation. Re-register when rowCount changes.
  useEffect(() => {
    return registerKeymap([
      {
        key: 'ArrowDown',
        scope: 'modal',
        when: () => getState().openModal === 'weekly',
        action: () => setSelectedIndex((i) => Math.min(i + 1, Math.max(0, rowCount - 1))),
      },
      {
        key: 'ArrowUp',
        scope: 'modal',
        when: () => getState().openModal === 'weekly',
        action: () => setSelectedIndex((i) => Math.max(i - 1, 0)),
      },
    ]);
  }, [rowCount]);

  const headerExtras = (
    <ShareIcon
      panel="weekly"
      panelLabel="Weekly"
      triggerId="weekly-modal"
      onClick={() => dispatch(openShareModal('weekly', 'weekly-modal'))}
    />
  );

  if (rowCount === 0) {
    return (
      <Modal
        title="Weekly history · last 12"
        accentClass="accent-cyan"
        headerExtras={headerExtras}
      >
        <div className="panel-empty">No usage history yet.</div>
      </Modal>
    );
  }

  const row = rows[Math.min(selectedIndex, rowCount - 1)];

  return (
    <Modal
      title="Weekly history · last 12"
      accentClass="accent-cyan"
      headerExtras={headerExtras}
    >
      <PeriodDetailCard row={row} variant="weekly" accentClass="accent-cyan" />
      <PeriodTable
        rows={rows}
        variant="weekly"
        accentClass="accent-cyan"
        selectedIndex={selectedIndex}
        onSelect={setSelectedIndex}
      />
    </Modal>
  );
}
