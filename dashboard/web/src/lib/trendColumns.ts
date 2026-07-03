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
  // S3 (#264) Cost column. Lives in TREND_COLUMNS as the single source of
  // truth, but is MODAL-ONLY: the always-visible panel card renders a subset
  // (PANEL_TREND_COLUMNS below) that omits it so the small tile stays
  // uncrowded. `nullKey` parks weeks with no cost row (null) at the END
  // regardless of asc/desc — the comparator never sees a null.
  { id: 'cost_usd',       label: 'Cost',         defaultDirection: 'desc', numeric: true,
    nullKey: (r) => r.cost_usd ?? null,
    compare: (a, b) => (a.cost_usd ?? 0) - (b.cost_usd ?? 0),
    className: 'c-cost',
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
