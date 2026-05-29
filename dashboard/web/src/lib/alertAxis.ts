import type { AlertAxis } from '../types/envelope';

// Shared three-way alert-axis labels (issue #19 widened the binary
// weekly|five_hour union with a third `budget` axis). Single source of
// truth so Toast / RecentAlertsPanel / RecentAlertsModal never drift on
// the chip text. The chip uses the SHOUT form; the title uses the
// sentence-case form.
export const AXIS_CHIP_LABEL: Record<AlertAxis, string> = {
  weekly: 'WEEKLY',
  five_hour: '5H-BLOCK',
  budget: 'BUDGET',
};

export const AXIS_TITLE_LABEL: Record<AlertAxis, string> = {
  weekly: 'Weekly',
  five_hour: '5h-block',
  budget: 'Budget',
};
