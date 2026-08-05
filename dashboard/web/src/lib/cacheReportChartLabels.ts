import type { CacheReportDailyRow } from '../types/envelope';

/**
 * Describe the report window once for every Cache Report chart (#469).
 *
 * Every retained row owns an x-axis slot. An explicitly unobserved row is not
 * a measurement, so name that subset when it differs from the full window.
 */
export function cacheReportChartDayCount(
  days: readonly CacheReportDailyRow[],
): string {
  const windowDays = days.length;
  const measuredDays = days.filter((day) => day.observed !== false).length;
  const windowLabel = `${windowDays} ${windowDays === 1 ? 'day' : 'days'}`;
  return measuredDays === windowDays
    ? windowLabel
    : `${windowLabel}, ${measuredDays} measured`;
}
