import { PeriodModal } from './PeriodModal';

export function DailyModal() {
  return (
    <PeriodModal
      variant="day" accentClass="accent-indigo" sharePanel="daily"
      modalKind="daily" panelLabel="Daily" triggerId="daily-modal"
    />
  );
}
