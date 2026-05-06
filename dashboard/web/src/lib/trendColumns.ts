import type { TrendChartDatum } from '../store/selectors';
import type { TableColumn } from './tableSort';

// Decorated row type. The Week column's correct sort across year boundaries
// requires a chronological index; envelope rows have only an MM-DD label.
// TrendPanel produces decorated rows via baseData.map((r, i) => ({ ...r, _chronoIdx: i })).
export type TrendTableRow = TrendChartDatum & { _chronoIdx: number };

export const TREND_COLUMNS: TableColumn<TrendTableRow>[] = [
  { id: 'week',           label: 'Week',         defaultDirection: 'desc',
    compare: (a, b) => a._chronoIdx - b._chronoIdx,
    className: 'c-week',
  },
  { id: 'used_pct',       label: 'Used%',        defaultDirection: 'desc', numeric: true,
    compare: (a, b) => (a.used_pct ?? 0) - (b.used_pct ?? 0),
    className: 'c-used',
  },
  { id: 'dollar_per_pct', label: '$/1%',         defaultDirection: 'desc', numeric: true,
    compare: (a, b) => (a.dollar_per_pct ?? 0) - (b.dollar_per_pct ?? 0),
    className: 'c-dollar',
  },
  { id: 'delta',          label: 'Δ (vs prior)', defaultDirection: 'desc', numeric: true,
    compare: (a, b) => (a.delta ?? 0) - (b.delta ?? 0),
    className: 'c-delta',
  },
];
