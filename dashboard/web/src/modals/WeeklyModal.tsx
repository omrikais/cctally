import { PeriodModal } from './PeriodModal';

export function WeeklyModal() {
  return (
    <PeriodModal
      variant="week" accentClass="accent-cyan" sharePanel="weekly"
      modalKind="weekly" panelLabel="Weekly" triggerId="weekly-modal" wide
    />
  );
}
