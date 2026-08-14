import { useState, useSyncExternalStore } from 'react';
import { Modal } from './Modal';
import { PeriodMiniBars, type PeriodNavRow } from './PeriodMiniBars';
import { PeriodDetailCard } from './PeriodDetailCard';
import { PeriodTable } from './PeriodTable';
import { KeyHintFooter } from '../components/KeyHintFooter';
import { ShareIcon } from '../components/ShareIcon';
import { useScopedSnapshot } from '../hooks/useScopedSnapshot';
import { useKeymap } from '../hooks/useKeymap';
import { keyOf, stepPeriod, type PeriodVariant } from './periodNav';
import { dailyToPeriodRow } from './historyData';
import { dispatch, getState, subscribeStore, topmostStoreFocusLayer, type ModalKind } from '../store/store';
import { openShareModal } from '../store/shareSlice';
import {
  presentationDailyLegs,
  presentationDailyRows,
  presentationPeriodRows,
  type AggregateWithheld,
} from '../lib/dashboardPresentation';
import { withheldMessage } from '../lib/withheldCopy';
import type { DashboardSelection, Envelope, PeriodRow } from '../types/envelope';
import type { SharePanelId } from '../share/types';

/**
 * PeriodModal (#264 S2) — the shared Daily / Weekly / Monthly detail modal.
 * Replaces the S8 HistoryModal: one variant, no Day·Week·Month toggle. Daily
 * renders navigator + detail only; Weekly/Monthly render a wide two-pane body
 * (navigator on top; detail LEFT, sortable table RIGHT). Every dataset already
 * ships in the SSE snapshot.
 */

type Variant = 'day' | 'week' | 'month';

interface Props {
  variant: Variant;
  accentClass: 'accent-indigo' | 'accent-cyan' | 'accent-pink';
  sharePanel: SharePanelId;   // 'daily' | 'weekly' | 'monthly'
  modalKind: ModalKind;       // 'daily' | 'weekly' | 'monthly'
  panelLabel: string;         // 'Daily' | 'Weekly' | 'Monthly'
  triggerId: string;          // '<panel>-modal'
  wide?: boolean;
  // #556 S4 — the >=1025px internal-pane scroll contract, separate from
  // `wide`. Weekly and Monthly render `.period-two-pane` and opt in; Daily
  // renders navigator + detail only and must not.
  paneScroll?: boolean;
}

const UNIT_PLURAL: Record<Variant, string> = { day: 'days', week: 'weeks', month: 'months' };
const DETAIL_VARIANT: Record<Variant, 'daily' | 'weekly' | 'monthly'> = { day: 'daily', week: 'weekly', month: 'monthly' };

function weeklyVocabulary(source: DashboardSelection) {
  if (source === 'codex') {
    return { plural: 'cycles', noun: 'cycle', window: 'Reset cycle', column: 'Cycle', nav: 'cycle' as const };
  }
  if (source === 'all') {
    return {
      plural: 'provider periods', noun: 'provider period', window: 'Provider window',
      column: 'Provider period', nav: 'provider period' as const,
    };
  }
  return { plural: 'weeks', noun: 'week', window: 'Subscription window', column: 'Week', nav: undefined };
}

interface Keyed { key: string; nav: PeriodNavRow; period: PeriodRow; }

// #556 S2 §3.7 — the modal renders the same three states its panel does.
//
// `buildKeyed` used to map a non-available daily outcome to `[]`, and the
// modal's empty branch then said "No usage history yet." — the honest-emptiness
// reading of a real fault, and the very copy the panel beside it had just
// stopped showing. Opening the modal one keystroke later contradicted the
// panel. The Projects modal already carried this state; Daily did not.
interface KeyedResult { rows: Keyed[]; withheld: AggregateWithheld | null; }

function buildKeyed(variant: Variant, env: Envelope | null, source: DashboardSelection): KeyedResult {
  if (variant === 'day') {
    const daily = presentationDailyRows(env, source);
    if (daily.state !== 'available') return { rows: [], withheld: daily };
    const rows = daily.rows;
    return { rows: rows.map((r, i) => ({
      key: r.date,
      nav: { key: r.date, label: r.label, cost: r.cost_usd, isCurrent: r.is_today, isEmpty: r.cost_usd <= 0 },
      period: dailyToPeriodRow(r, rows[i + 1]),
    })), withheld: null };
  }
  const rows = presentationPeriodRows(env, source, variant === 'week' ? 'weekly' : 'monthly');
  const v: PeriodVariant = variant;
  return { withheld: null, rows: rows.map((r) => ({
    key: keyOf(r, v),
    nav: {
      key: keyOf(r, v),
      // #556 S2 §5.4 — the navigator qualifies BOTH period kinds now, for the
      // same reason the table does: unmerged monthly rows can share a label.
      label: source === 'all' && r.source != null
        ? `${r.source === 'claude' ? 'Claude' : 'Codex'} · ${r.label}`
        : r.label,
      cost: r.cost_usd,
      isCurrent: r.is_current,
      isEmpty: r.cost_usd <= 0,
    },
    period: r,
  })) };
}

export function PeriodModal({ variant, accentClass, sharePanel, modalKind, panelLabel, triggerId, wide, paneScroll }: Props) {
  // Bound when OPEN_MODAL fires. A source switch changes the board behind the
  // modal, never the period rows or share target already in front of the user.
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  // #416 — the expansion of a scoped panel stays scoped. `DailyPanel` /
  // `WeeklyPanel` / `MonthlyPanel` all read `useScopedSnapshot()`, so reading
  // the unscoped snapshot here painted every account's rows under the focused
  // account's chip.
  const env = useScopedSnapshot(source);

  // Day seeds from a heatmap-cell deep-link (openDailyDate); week/month seed
  // null → effectiveKey clamps to the first (current) row.
  const [selectedKey, setSelectedKey] = useState<string | null>(
    () => (variant === 'day' ? getState().openDailyDate : null),
  );

  const { rows: keyed, withheld } = buildKeyed(variant, env, source);
  const navRows = keyed.map((k) => k.nav);
  const periodRows = keyed.map((k) => k.period);

  const keySet = new Set(keyed.map((k) => k.key));
  const firstKey = keyed[0]?.key ?? null;
  const effectiveKey = selectedKey != null && keySet.has(selectedKey) ? selectedKey : firstKey;
  const selectedRow = keyed.find((k) => k.key === effectiveKey)?.period ?? null;
  const vocabulary = weeklyVocabulary(source);

  // ↑/↓ steps the navigator's chronological order (same order PeriodMiniBars'
  // ‹/› uses), so the two steppers always agree even under a table sort.
  const orderedKeys = navRows.map((n) => ({ key: n.key }));

  // Gate on the store focus layer (Codex finding 1): only when THIS panel modal
  // is the topmost store layer — so a header-chip-opened update/doctor modal or
  // a share/composer overlay above it suspends the arrows.
  const isTopmost = (): boolean =>
    topmostStoreFocusLayer(getState()) === 'panel' && getState().openModal === modalKind;
  useKeymap([
    {
      key: 'ArrowDown', scope: 'modal', when: isTopmost,
      action: () => { const t = stepPeriod(orderedKeys, effectiveKey, 'older'); if (t) setSelectedKey(t); },
    },
    {
      key: 'ArrowUp', scope: 'modal', when: isTopmost,
      action: () => { const t = stepPeriod(orderedKeys, effectiveKey, 'newer'); if (t) setSelectedKey(t); },
    },
  ]);

  const headerExtras = (
    <ShareIcon
      panel={sharePanel}
      panelLabel={panelLabel}
      triggerId={triggerId}
      onClick={() => dispatch(openShareModal(sharePanel, triggerId))}
    />
  );

  // Under All neither weekly nor monthly counts what its unit noun implies:
  // both are unmerged, so one calendar month or one wall-clock week
  // contributes a row per provider. Weekly already said "N provider periods";
  // monthly said "last 10 months" over a count of ten PROVIDER-months, which
  // the panel footer's span then contradicted (2026-01 – 2026-08 is eight).
  // Neither drops "last", because neither is a trailing-N-units window.
  const monthlyPlural = navRows.length === 1 ? 'provider month' : 'provider months';
  const title = navRows.length > 0
    ? source === 'all' && variant === 'week'
      ? `${panelLabel} · ${navRows.length} ${vocabulary.plural}`
      : source === 'all' && variant === 'month'
        ? `${panelLabel} · ${navRows.length} ${monthlyPlural}`
        : `${panelLabel} · last ${navRows.length} ${variant === 'week' ? vocabulary.plural : UNIT_PLURAL[variant]}`
    : panelLabel;

  // #556 S2 §6.3 — the modal owns SELECTION, so it resolves the legs for the
  // selected day and hands them to the card. Only under All and only for the
  // daily variant: the single-provider tabs have nothing to break down, and
  // weekly/monthly rows are never merged.
  const dailyLegs = variant === 'day' && source === 'all' && effectiveKey != null
    ? presentationDailyLegs(env, effectiveKey)
    : undefined;

  const detail = selectedRow && (
    <PeriodDetailCard
      row={selectedRow}
      variant={DETAIL_VARIANT[variant]}
      accentClass={accentClass}
      periodNoun={variant === 'week' ? vocabulary.noun : undefined}
      windowLabel={variant === 'week' ? vocabulary.window : undefined}
      providerLegs={dailyLegs}
    />
  );

  return (
    <Modal title={title} accentClass={accentClass} headerExtras={headerExtras} wide={wide} paneScroll={paneScroll} dataSource={source}>
      {withheld ? (
        <div className="panel-empty panel-withheld" data-withheld-code={withheld.code}>
          {withheldMessage(withheld, 'history')}
        </div>
      ) : navRows.length === 0 ? (
        <div className="panel-empty">No usage history yet.</div>
      ) : (
        <>
          <PeriodMiniBars
            unit={variant}
            displayUnit={variant === 'week' ? vocabulary.nav : undefined}
            rows={navRows}
            selectedKey={effectiveKey}
            onSelect={setSelectedKey}
          />
          {variant === 'day' ? (
            detail
          ) : (
            <div className="period-two-pane">
              <div className="period-detail-pane">{detail}</div>
              <div className="period-table-pane">
                <PeriodTable
                  rows={periodRows}
                  variant={variant === 'week' ? 'weekly' : 'monthly'}
                  accentClass={accentClass}
                  selectedKey={effectiveKey}
                  onSelect={setSelectedKey}
                  // #556 S2 §5.4 — MONTHLY gets `showSource` too. Its rows
                  // are no longer merged into `source: 'all'`, so two rows can
                  // now share a label and the provider column is what tells
                  // them apart.
                  showSource={source === 'all'}
                  periodLabel={variant === 'week' ? vocabulary.column : undefined}
                />
              </div>
            </div>
          )}
        </>
      )}
      <KeyHintFooter
        hints={[
          { keys: <kbd>↑↓</kbd>, label: 'period' },
          { keys: <kbd>Esc</kbd>, label: 'close' },
        ]}
      />
    </Modal>
  );
}
