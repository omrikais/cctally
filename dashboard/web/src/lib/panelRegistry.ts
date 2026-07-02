import type * as React from 'react';
import { ForecastPanel } from '../panels/ForecastPanel';
import { TrendPanel } from '../panels/TrendPanel';
import { SessionsPanel } from '../panels/SessionsPanel';
import { ProjectsPanel } from '../panels/ProjectsPanel';
import { BlocksPanel } from '../panels/BlocksPanel';
// S2 (#264): the three period cards are independent grid tiles again — the
// DailyPanel heatmap (Daily), plus the restored WeeklyPanel / MonthlyPanel,
// each opening its own modal at its own period.
import { DailyPanel } from '../panels/DailyPanel';
import { WeeklyPanel } from '../panels/WeeklyPanel';
import { MonthlyPanel } from '../panels/MonthlyPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { CacheReportPanel } from '../panels/CacheReportPanel';
import { dispatch } from '../store/store';
import {
  openActiveOrNewestBlockModal,
  openMostRecentSessionModal,
} from '../store/actions';
import { DEFAULT_PANEL_ORDER, type PanelId, type GridPanelId } from './panelIds';

// Re-export for backward compatibility — most callers import from
// panelRegistry. The underlying definitions live in panelIds.ts to break
// the circular import with store/store.ts.
export { DEFAULT_PANEL_ORDER };
export type { PanelId, GridPanelId };

export interface PanelDef {
  // #248: grid cards are GridPanelId — `current-week` left the grid (it is
  // the hero), so the registry never carries it. `PanelId` is kept for the
  // modal/share path (CurrentWeekModal / SharePanelId).
  id: GridPanelId;
  label: string;
  Component: React.ComponentType;
  openAction: () => void;
}

export const PANEL_REGISTRY: Record<GridPanelId, PanelDef> = {
  forecast: {
    id: 'forecast',
    label: 'Forecast',
    Component: ForecastPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'forecast' }),
  },
  trend: {
    id: 'trend',
    label: 'Trend',
    Component: TrendPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'trend' }),
  },
  sessions: {
    id: 'sessions',
    label: 'Sessions',
    Component: SessionsPanel,
    openAction: openMostRecentSessionModal,
  },
  projects: {
    id: 'projects',
    label: 'Projects',
    Component: ProjectsPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'projects' }),
  },
  blocks: {
    id: 'blocks',
    label: 'Blocks',
    Component: BlocksPanel,
    openAction: openActiveOrNewestBlockModal,
  },
  // S2 (#264): three independent period cards. Daily is the heatmap (a
  // heatmap-cell click deep-links to that day via OPEN_MODAL { dailyDate });
  // Weekly/Monthly are the restored summary tiles, each opening its own
  // wide two-pane modal.
  daily: {
    id: 'daily',
    label: 'Daily',
    Component: DailyPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }),
  },
  weekly: {
    id: 'weekly',
    label: 'Weekly',
    Component: WeeklyPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'weekly' }),
  },
  monthly: {
    id: 'monthly',
    label: 'Monthly',
    Component: MonthlyPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'monthly' }),
  },
  alerts: {
    id: 'alerts',
    label: 'Recent alerts',
    Component: RecentAlertsPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'alerts' }),
  },
  'cache-report': {
    id: 'cache-report',
    label: 'Cache Report',
    Component: CacheReportPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'cache-report' }),
  },
};
