import { useEffect, useState } from 'react';
import { Modal } from './Modal';
import { PeriodDetailCard } from './PeriodDetailCard';
import { PeriodTable } from './PeriodTable';
import { useSnapshot } from '../hooks/useSnapshot';
import { registerKeymap } from '../store/keymap';
import { getState } from '../store/store';

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

  if (rowCount === 0) {
    return (
      <Modal title="Monthly history · last 12" accentClass="accent-pink">
        <div className="panel-empty">No usage history yet.</div>
      </Modal>
    );
  }

  const row = rows[Math.min(selectedIndex, rowCount - 1)];

  return (
    <Modal title="Monthly history · last 12" accentClass="accent-pink">
      <PeriodDetailCard row={row} variant="monthly" accentClass="accent-pink" />
      <PeriodTable
        rows={rows}
        variant="monthly"
        accentClass="accent-pink"
        selectedIndex={selectedIndex}
        onSelect={setSelectedIndex}
      />
    </Modal>
  );
}
