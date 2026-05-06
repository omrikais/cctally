import { useSyncExternalStore } from 'react';
import { getState, subscribeStore } from '../store/store';
import { CurrentWeekModal } from './CurrentWeekModal';
import { ForecastModal } from './ForecastModal';
import { TrendModal } from './TrendModal';
import { SessionModal } from './SessionModal';
import { WeeklyModal } from './WeeklyModal';
import { MonthlyModal } from './MonthlyModal';
import { BlockModal } from './BlockModal';
import { DailyModal } from './DailyModal';
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
    case 'weekly':
      return <WeeklyModal />;
    case 'monthly':
      return <MonthlyModal />;
    case 'block':
      return <BlockModal />;
    case 'daily':
      return <DailyModal />;
    case 'alerts':
      return <RecentAlertsModal />;
  }
}
