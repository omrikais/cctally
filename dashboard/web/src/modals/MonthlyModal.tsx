import { PeriodModal } from './PeriodModal';

export function MonthlyModal() {
  return (
    <PeriodModal
      variant="month" accentClass="accent-pink" sharePanel="monthly"
      modalKind="monthly" panelLabel="Monthly" triggerId="monthly-modal" wide
    />
  );
}
