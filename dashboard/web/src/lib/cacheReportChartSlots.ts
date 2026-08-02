export type CacheReportChartSize = 'mini' | 'large';

const SLOT_LAYOUT = {
  mini: { chartWidth: 272, padX: 0, gap: 1 },
  large: { chartWidth: 800, padX: 28, gap: 4 },
} as const;

export interface CacheReportChartSlot {
  chartWidth: number;
  left: number;
  center: number;
  right: number;
  width: number;
}

/**
 * The one x-axis contract shared by Cache Report's sparkline and net bars.
 * Every retained day owns a band, including an unobserved day; charts may
 * omit its mark, but they must not collapse or shift its slot.
 */
export function cacheReportChartSlot(
  size: CacheReportChartSize,
  index: number,
  count: number,
): CacheReportChartSlot {
  if (!Number.isInteger(count) || count < 1) {
    throw new RangeError('cache report chart count must be a positive integer');
  }
  if (!Number.isInteger(index) || index < 0 || index >= count) {
    throw new RangeError('cache report chart index must address an existing slot');
  }
  const { chartWidth, padX, gap } = SLOT_LAYOUT[size];
  const width = (chartWidth - (2 * padX) - (gap * (count - 1))) / count;
  const left = padX + index * (width + gap);
  return {
    chartWidth,
    left,
    center: left + width / 2,
    right: left + width,
    width,
  };
}
