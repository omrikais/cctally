import type * as React from 'react';
import { CurrentWeekPanel } from '../panels/CurrentWeekPanel';
import { ForecastPanel } from '../panels/ForecastPanel';
import { TrendPanel } from '../panels/TrendPanel';
import { SessionsPanel } from '../panels/SessionsPanel';
import { WeeklyPanel } from '../panels/WeeklyPanel';
import { MonthlyPanel } from '../panels/MonthlyPanel';
import { BlocksPanel } from '../panels/BlocksPanel';
import { DailyPanel } from '../panels/DailyPanel';
import { RecentAlertsPanel } from '../components/RecentAlertsPanel';
import { dispatch } from '../store/store';
import {
  openActiveOrNewestBlockModal,
  openMostRecentSessionModal,
} from '../store/actions';
import { DEFAULT_PANEL_ORDER, type PanelId } from './panelIds';

// Re-export for backward compatibility — most callers import from
// panelRegistry. The underlying definitions live in panelIds.ts to break
// the circular import with store/store.ts.
export { DEFAULT_PANEL_ORDER };
export type { PanelId };

export interface PanelDef {
  id: PanelId;
  label: string;
  Component: React.ComponentType;
  openAction: () => void;
}

export const PANEL_REGISTRY: Record<PanelId, PanelDef> = {
  'current-week': {
    id: 'current-week',
    label: 'Current Week',
    Component: CurrentWeekPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'current-week' }),
  },
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
  blocks: {
    id: 'blocks',
    label: 'Blocks',
    Component: BlocksPanel,
    openAction: openActiveOrNewestBlockModal,
  },
  daily: {
    id: 'daily',
    label: 'Daily',
    Component: DailyPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'daily' }),
  },
  alerts: {
    id: 'alerts',
    label: 'Recent alerts',
    Component: RecentAlertsPanel,
    openAction: () => dispatch({ type: 'OPEN_MODAL', kind: 'alerts' }),
  },
};
