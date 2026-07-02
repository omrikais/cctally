import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { CurrentWeekModal } from './CurrentWeekModal';
import { ForecastModal } from './ForecastModal';
import { TrendModal } from './TrendModal';
import { SessionModal } from './SessionModal';
import { DailyModal } from './DailyModal';
import { WeeklyModal } from './WeeklyModal';
import { MonthlyModal } from './MonthlyModal';
import { BlockModal } from './BlockModal';
import { ProjectsModal } from './ProjectsModal';
import { CacheReportModal } from './CacheReportModal';
import { RecentAlertsModal } from '../components/RecentAlertsModal';

export function ModalRoot() {
  const kind = useSyncExternalStore(subscribeStore, () => getState().openModal);
  if (!kind) return null;

  switch (kind) {
    case 'current-week':
      return <CurrentWeekModal />;
    case 'forecast':
      return <ForecastModal />;
    case 'trend':
      return <TrendModal />;
    case 'session':
      return <SessionModal />;
    case 'daily':
      return <DailyModal />;
    case 'weekly':
      return <WeeklyModal />;
    case 'monthly':
      return <MonthlyModal />;
    case 'block':
      return <BlockModal />;
    case 'projects':
      return <ProjectsModal />;
    case 'alerts':
      return <RecentAlertsModal />;
    case 'cache-report':
      return <CacheReportModal />;
  }
}
