import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt, type FmtCtx } from '../lib/fmt';
import {
  presentationBudgetComposition,
  type ProviderPresentationSection,
} from '../lib/dashboardPresentation';
import { SourceChip } from '../panels/sourcePanel';
import {
  alertNavigation,
  envelopeNow,
} from '../lib/alertScope';
import { followAlertTarget } from '../store/followAlertTarget';

import type {
  BudgetPresentation,
  DashboardSelection,
  Envelope,
  SourceName,
} from '../types/envelope';

// #556 S5 §4 — the provider-labelled configured-BUDGET block.
//
// One component, three hosts: the Forecast panel and the Forecast modal, on
// All and on both provider tabs (operator decision 4). It renders the
// configured budget — an amount the user set, measured over a configured
// period — and never the quota projection. The two verdict vocabularies are
// deliberately kept apart (§4.5): `ok`/`cap`/`capped` describes the forecast
// and stays in the forecast region; `ok`/`warn`/`over` describes the budget and
// renders only here.
//
// NOTHING is composed across providers. Under All this is two independent
// cards, each naming its own provider and its own period; there is no combined
// budget figure anywhere, and `sources.all.capabilities.budget` still says
// `not_applicable` / `provider-native` for exactly that reason.

// The provider-correct command for an unconfigured budget (operator decision 5).
const SET_COMMAND: Record<SourceName, string> = {
  claude: 'cctally budget set <amount>',
  codex: 'cctally budget set <amount> --vendor codex',
};

// A deliberate divergence from the CLI string (§4.6): `cctally budget` prints
// "No weekly budget set", and the dashboard drops "weekly" because
// `docs/commands/budget.md` calls `weekly_usd` a back-compat misnomer and the
// block already names the period beside it.
const UNSET_COPY = 'No budget set.';
const ACCOUNT_ONLY_COPY = 'No provider-wide budget set.';
const ACCOUNT_UNSET_COPY = 'No budget set for this account.';

const VERDICT_CLASS: Record<string, string> = {
  ok: 'good',
  warn: 'warn',
  over: 'over',
};

function BudgetUnconfigured({
  source,
  disposition,
  accountKey,
  configuredAccounts,
}: {
  source: SourceName;
  disposition: string;
  accountKey?: string;
  configuredAccounts?: Record<string, number>;
}) {
  // Rendering "No budget set." to a user who HAS one set is a lie, which is why
  // the server distinguishes these at all. An unrecognised disposition from a
  // newer server gets generic copy rather than either specific claim — the
  // required fallback branch (S2's closed-for-the-server, open-for-the-client
  // rule).
  if (disposition === 'account_budgets_only') {
    return (
      <div className="budget-empty" data-testid={`budget-empty-${source}`}>
        <p className="budget-empty-copy">{ACCOUNT_ONLY_COPY}</p>
        <p className="budget-empty-hint">
          Per-account budgets are configured — see the account cards.
        </p>
      </div>
    );
  }
  if (disposition === 'account_budget_unset' && accountKey != null) {
    // `config set` replaces the JSON map; carry every sibling forward so the
    // example fixes this card without silently unsetting another one (#586).
    const updatedAccounts = { ...(configuredAccounts ?? {}), [accountKey]: 30 };
    const command = `cctally config set budget.codex.accounts '${JSON.stringify(updatedAccounts)}'`;
    return (
      <div className="budget-empty" data-testid={`budget-empty-${source}`}>
        <p className="budget-empty-copy">{ACCOUNT_UNSET_COPY}</p>
        <p className="budget-empty-hint">Edit the per-account budget map for this key:</p>
        <p className="budget-empty-hint">
          <code className="budget-empty-cmd">{command}</code>
        </p>
      </div>
    );
  }
  if (disposition === 'provider_budget_unset') {
    return (
      <div className="budget-empty" data-testid={`budget-empty-${source}`}>
        <p className="budget-empty-copy">{UNSET_COPY}</p>
        <p className="budget-empty-hint">
          <code className="budget-empty-cmd">{SET_COMMAND[source]}</code>
        </p>
      </div>
    );
  }
  return (
    <div className="budget-empty" data-testid={`budget-empty-${source}`}>
      <p className="budget-empty-copy">No budget status to show.</p>
    </div>
  );
}

function BudgetUnavailable({
  source,
  presentation,
  detailed,
}: {
  source: SourceName;
  presentation: Extract<BudgetPresentation, { state: 'unavailable' }>;
  detailed: boolean;
}) {
  // Never a fabricated percentage or verdict. `period_unresolved` in particular
  // is a CONFIGURED budget whose window cannot be resolved yet — the state the
  // CLI reports as `status: "no_data"` — and it must not read as "unset".
  //
  // #556 S5 §4.6 / Unit 2 review F5 — an unavailable state renders what the
  // user CONFIGURED, with the window named as unresolved. The block used to
  // print the bare code and neither the amount nor the server's sentence, so
  // the one state §4.6 describes in words was the one it said least about.
  // Both fields are optional on the wire, so each is rendered only when sent.
  const { reason, unavailable } = presentation;
  const copy = reason === 'period_unresolved'
    ? 'Budget period not resolved yet, so no status is published.'
    : reason === 'budget_compute_failed'
      ? 'Budget status could not be computed.'
      : 'Budget status is unavailable.';
  const amount = unavailable.budget_usd;
  const period = unavailable.period;
  return (
    <div className="budget-empty" data-testid={`budget-unavailable-${source}`}>
      {amount != null && (
        <>
          <div className="budget-figures">
            {/* #556 S5 browser-QA P2 — `is-lone`: this is the card's only
                figure, so it carries the headline's prominence rather than the
                dim treatment `.budget-target` takes as the right-hand half of
                "spend of target". */}
            <span className="budget-target is-lone" data-testid={`budget-target-${source}`}>
              {detailed ? fmt.usd2(amount) : fmt.usd0(amount)}
            </span>
            <span className="budget-of">configured</span>
            {/* Last, because `.budget-period-chip` carries `margin-left: auto`
                and must stay the right-hand end of the row as it does above. */}
            {period != null && (
              <span className="budget-period-chip" data-testid={`budget-period-${source}`}>
                {period}
              </span>
            )}
          </div>
          {/* #556 S5 browser-QA P2 — on its own line, not inside the figures
              row: four items wrapped that row at panel width and pushed the
              period chip onto a line of its own. Still deliberately not a
              verdict chip (§4.6) — it is plain amber text, and it only occupies
              the line a verdict would. */}
          <div className="budget-window-unresolved" data-testid={`budget-window-${source}`}>
            window unresolved
          </div>
        </>
      )}
      <p className="budget-empty-copy">{copy}</p>
      {unavailable.message !== '' && (
        <p className="budget-empty-hint" data-testid={`budget-message-${source}`}>
          {unavailable.message}
        </p>
      )}
      <p className="budget-empty-hint" data-testid={`budget-reason-${source}`}>{reason}</p>
    </div>
  );
}

// #620 S1 D12 — a warn or over budget is a live warning state, so it routes to
// the surface that explains the period it measures. The scope is derived
// through the SAME kernel the alert rows use, from a context built out of the
// status's own retained `period` and `window_start_at`, so the budget block and
// an alert about that budget cannot disagree about which window they mean.
function BudgetExplain({
  source,
  status,
  env,
}: {
  source: SourceName;
  status: Extract<BudgetPresentation, { state: 'configured' }>['status'];
  env: Envelope | null;
}) {
  if (status.verdict !== 'warn' && status.verdict !== 'over') return null;
  const nav = alertNavigation(
    {
      // Not a real alert row: no id, no threshold crossing. The axis and the
      // context are what the kernel reads, and they are exactly the axis and
      // the window this block describes.
      id: '',
      axis: source === 'codex' ? 'codex_budget' : 'budget',
      threshold: 0,
      crossed_at: '',
      alerted_at: '',
      context: { period: status.period, period_start_at: status.window_start_at },
    },
    env,
    envelopeNow(env),
  );
  if (!nav.available || nav.target == null) {
    return (
      <span className="alert-row-withheld" data-testid={`budget-withheld-${source}`}>
        {nav.withheldReason}
      </span>
    );
  }
  const target = nav.target;
  return (
    <button
      type="button"
      className="alert-row-open budget-explain"
      data-testid={`budget-explain-${source}`}
      onClick={(e) => {
        e.stopPropagation();
        followAlertTarget(target);
      }}
    >
      {target.label}
    </button>
  );
}

function BudgetFigures({
  source,
  presentation,
  detailed,
  ctx,
  env,
}: {
  source: SourceName;
  presentation: Extract<BudgetPresentation, { state: 'configured' }>;
  detailed: boolean;
  ctx: FmtCtx;
  env: Envelope | null;
}) {
  const status = presentation.status;
  const cls = VERDICT_CLASS[status.verdict] ?? 'good';
  const fill = Math.min(100, Math.max(0, status.consumption_pct));
  return (
    <>
      <div className="budget-figures">
        <span className="budget-spend" data-testid={`budget-spend-${source}`}>
          {detailed ? fmt.usd2(status.spent_usd) : fmt.usd0(status.spent_usd)}
        </span>
        <span className="budget-of">of</span>
        <span className="budget-target" data-testid={`budget-target-${source}`}>
          {detailed ? fmt.usd2(status.budget_usd) : fmt.usd0(status.budget_usd)}
        </span>
        <span className="budget-period-chip" data-testid={`budget-period-${source}`}>
          {status.period}
        </span>
      </div>
      {/* Decorative: the percent and the verdict beside it carry the value for
          assistive technology, exactly as the forecast pace bar does. */}
      <div className={`budget-bar is-${cls}`} role="presentation">
        <div className="budget-bar-fill" style={{ width: `${fill}%` }} />
      </div>
      <div className="budget-verdict-line">
        <span className="budget-consumption" data-testid={`budget-consumption-${source}`}>
          {detailed ? fmt.pct1(status.consumption_pct) : fmt.pct0(status.consumption_pct)}
        </span>
        <span className={`budget-verdict-chip is-${cls}`} data-testid={`budget-verdict-${source}`}>
          {status.verdict}
        </span>
        {status.low_confidence && (
          <span className="budget-lowconf" data-testid={`budget-lowconf-${source}`}>
            low confidence
          </span>
        )}
        <BudgetExplain source={source} status={status} env={env} />
      </div>
      <div className="fc-budget-foot budget-foot">
        {/* #556 S5 §4.7 — `Budget pace` MOVED here from the forecast footer and
            is rendered exactly once. The two Claude quota-ceiling rows and the
            Codex `Confidence` row stay in the forecast footer: they qualify the
            projection, not the configured budget. */}
        <div className="fc-foot-line">
          <span className="fc-foot-k">Budget pace</span>
          <span className="fc-foot-v" data-testid={`budget-pace-${source}`}>
            {fmt.usd2PerDay(status.pace.daily_usd)}
          </span>
        </div>
        {detailed && (
          <>
            <div className="fc-foot-line">
              <span className="fc-foot-k">Remaining</span>
              <span className="fc-foot-v">{fmt.usd2(status.remaining_usd)}</span>
            </div>
            <div className="fc-foot-line">
              <span className="fc-foot-k">Projection</span>
              <span className="fc-foot-v" data-testid={`budget-projection-${source}`}>
                {status.pace.projected_low_usd == null || status.pace.projected_high_usd == null
                  ? '—'
                  : `${fmt.usd2(status.pace.projected_low_usd)} – ${fmt.usd2(status.pace.projected_high_usd)}`}
              </span>
            </div>
            <div className="fc-foot-line">
              <span className="fc-foot-k">Recent-24h</span>
              <span className="fc-foot-v">{fmt.usd2(status.recent_24h_usd)}</span>
            </div>
            <div className="fc-foot-line">
              <span className="fc-foot-k">Alert thresholds</span>
              <span className="fc-foot-v" data-testid={`budget-thresholds-${source}`}>
                {status.alert_thresholds.length === 0
                  ? '—'
                  : status.alert_thresholds.map((n) => `${n}%`).join(' · ')}
              </span>
            </div>
            <div className="fc-foot-line">
              <span className="fc-foot-k">Period</span>
              <span className="fc-foot-v" data-testid={`budget-window-${source}`}>
                {fmt.datetimeShort(status.window_start_at, ctx)}
                {' → '}
                {fmt.datetimeShort(status.window_end_at, ctx)}
              </span>
            </div>
          </>
        )}
      </div>
    </>
  );
}

export function BudgetProviderSection({
  section,
  surface,
  ctx,
  env = null,
}: {
  section: ProviderPresentationSection<BudgetPresentation>;
  surface: 'panel' | 'modal';
  ctx: FmtCtx;
  env?: Envelope | null;
}) {
  // #556 S4's heading pattern, applied even when there is only one section: a
  // plain `<div>` takes no accessible name, so the region role is what makes
  // the visually-hidden heading name a landmark. The id is surface-qualified,
  // because the panel and the modal can be mounted at the same time.
  const headingId = `budget-${surface}-${section.source}-heading`;
  const presentation = section.value;
  return (
    <div
      className="source-provider-section provider-summary-card budget-provider-summary"
      // Deliberately NOT `data-provider-section`: that attribute selects the
      // FORECAST provider sections in the shipped S4 tests and in the browser
      // lane, and a budget card answering the same selector would silently
      // double every "there are two provider sections" assertion.
      data-budget-section={section.source}
      data-surface={surface}
      role="region"
      aria-labelledby={headingId}
    >
      <div className="source-provider-head">
        <h3 className="sr-only" id={headingId}>
          {section.label} budget
        </h3>
        <SourceChip source={section.source} />
        <strong className="budget-block-title">Budget</strong>
        {section.status !== 'available' && presentation == null && (
          <span className="provider-section-status">{section.status}</span>
        )}
      </div>
      {presentation == null ? (
        <div className="provider-section-reason">{section.reason}</div>
      ) : presentation.state === 'configured' ? (
        <BudgetFigures
          source={section.source}
          presentation={presentation}
          detailed={surface === 'modal'}
          ctx={ctx}
          env={env}
        />
      ) : presentation.state === 'not_configured' ? (
        <BudgetUnconfigured
          source={section.source}
          disposition={presentation.disposition}
          accountKey={presentation.accountKey}
          configuredAccounts={presentation.configuredAccounts}
        />
      ) : (
        <BudgetUnavailable
          source={section.source}
          presentation={presentation}
          detailed={surface === 'modal'}
        />
      )}
      {presentation != null && section.reason != null && (
        <div className="provider-section-reason">{section.reason}</div>
      )}
    </div>
  );
}

// The host-facing entry point. Under All this emits both provider sections in
// order (Claude first); on a provider tab it emits exactly one.
export function BudgetComposition({
  env,
  selection,
  surface,
}: {
  env: Envelope | null;
  selection: DashboardSelection;
  surface: 'panel' | 'modal';
}) {
  const display = useDisplayTz();
  const ctx: FmtCtx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const composition = presentationBudgetComposition(env, selection);
  return (
    <>
      {composition.sections.map((section) => (
        <BudgetProviderSection
          key={section.source}
          section={section}
          surface={surface}
          ctx={ctx}
          env={env}
        />
      ))}
    </>
  );
}
