import { act, cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import fixture from '../../__tests__/fixtures/envelope.json';
import { _resetForTests, dispatch, getState, updateSnapshot } from '../store/store';
import type { DashboardSelection, Envelope } from '../types/envelope';
import { TrendModal } from './TrendModal';
import { ProjectsModal } from './ProjectsModal';
import { CacheReportModal } from './CacheReportModal';
import { ForecastModal } from './ForecastModal';

const envelope = fixture as unknown as Envelope;

function renderFor(source: DashboardSelection, node: React.ReactElement) {
  act(() => {
    updateSnapshot(envelope);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source });
  });
  return render(node);
}

beforeEach(() => {
  localStorage.clear();
  _resetForTests();
});

afterEach(() => cleanup());

describe.each(['claude', 'codex', 'all'] as const)(
  'provider-neutral destination composition — %s',
  (source) => {
    it('keeps the canonical Trend hierarchy and explicit unavailable slots', () => {
      const { container } = renderFor(source, <TrendModal />);
      expect(container.querySelector('.modal-trend .m-chipstrip')).not.toBeNull();
      expect(container.querySelector('.modal-trend .m-hero')).not.toBeNull();
      expect(container.querySelector('.modal-trend .mtr-sparkhero')).not.toBeNull();
      expect(container.querySelector('.modal-trend .m-histable')).not.toBeNull();
      if (source !== 'claude') {
        expect(container.querySelector('.modal-trend .m-unavailable')).not.toBeNull();
      }
    });

    it('keeps Projects controls, visualization, table, and footer', () => {
      const { container } = renderFor(source, <ProjectsModal />);
      expect(container.querySelector('.projects-controls')).not.toBeNull();
      expect(
        container.querySelector('.projects-trend, [data-testid="projects-ranked-bars"]'),
      ).not.toBeNull();
      expect(container.querySelector('.projects-table')).not.toBeNull();
      expect(container.querySelector('.projects-modal-footer-hint')).not.toBeNull();
    });

    it('keeps all Cache Report composition slots', () => {
      const { container } = renderFor(source, <CacheReportModal />);
      expect(container.textContent).toContain("Today's spotlight");
      expect(container.textContent).toContain('Cache hit %');
      expect(container.textContent).toContain('Net $ per day');
      expect(container.textContent).toContain('Daily rows');
      expect(container.querySelector('[data-bd-kind="projects"]')).not.toBeNull();
      expect(container.querySelector('[data-bd-kind="models"]')).not.toBeNull();
      if (source !== 'claude') {
        expect(container.querySelector('.modal-cache-unavailable')).not.toBeNull();
      }
    });

    it('keeps Forecast verdict, hero, range, rates, and budget sections', () => {
      const { container } = renderFor(source, <ForecastModal />);
      expect(container.querySelector('.modal-forecast .m-chipstrip')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .m-hero')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .mfc-rangewrap')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .sec-rates')).not.toBeNull();
      expect(container.querySelector('.modal-forecast .sec-bud')).not.toBeNull();
      if (source !== 'claude') {
        expect(container.querySelector('.modal-forecast .m-unavailable')).not.toBeNull();
      }
    });
  },
);

it.each(['codex', 'all'] as const)(
  'routes a %s project row through the shared source-detail path',
  (source) => {
    const { getAllByTestId } = renderFor(source, <ProjectsModal />);
    fireEvent.click(getAllByTestId('projects-table-row')[0]);
    expect(getState().openSourceDetail).toMatchObject({ resource: 'project' });
  },
);

it('keeps the canonical Codex Forecast composition when native forecast data is unavailable', () => {
  const unavailable = structuredClone(envelope);
  if (unavailable.sources?.codex?.data?.quota) {
    unavailable.sources.codex.data.quota.histories = [];
  }
  act(() => {
    updateSnapshot(unavailable);
    dispatch({ type: 'SET_ACTIVE_SOURCE', source: 'codex' });
  });
  const { container } = render(<ForecastModal />);
  expect(container.textContent).toContain('Forecast unavailable');
  expect(container.querySelector('.modal-forecast .m-hero')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .mfc-rangewrap')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .sec-rates')).not.toBeNull();
  expect(container.querySelector('.modal-forecast .sec-bud')).not.toBeNull();
});
