import type { SessionRow } from '../types/envelope';
import type { TableColumn } from './tableSort';

export const SESSIONS_COLUMNS: TableColumn<SessionRow>[] = [
  { id: 'started',  label: 'Started', defaultDirection: 'desc',
    compare: (a, b) => {
      const ta = Date.parse(a.started_utc ?? '') || 0;
      const tb = Date.parse(b.started_utc ?? '') || 0;
      return ta - tb;
    },
  },
  { id: 'duration', label: 'Dur',     defaultDirection: 'desc',
    compare: (a, b) => (a.duration_min ?? 0) - (b.duration_min ?? 0),
  },
  { id: 'model',    label: 'Model',   defaultDirection: 'asc',
    compare: (a, b) => (a.model || '').localeCompare(b.model || ''),
  },
  { id: 'project',  label: 'Project', defaultDirection: 'asc',
    compare: (a, b) => (a.project || '').localeCompare(b.project || ''),
  },
  { id: 'cost',     label: 'Cost',    defaultDirection: 'desc', numeric: true,
    compare: (a, b) => (a.cost_usd ?? 0) - (b.cost_usd ?? 0),
  },
];
