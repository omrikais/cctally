import { beforeEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { BudgetComposition } from './BudgetBlock';
import { presentationBudgetComposition } from '../lib/dashboardPresentation';
import { _resetForTests } from '../store/store';
import {
  makeAllSourceEntry,
  makeClaudeBudgetStatus,
  makeClaudeSourceData,
  makeClaudeSourceEntry,
  makeCodexSourceData,
  makeCodexSourceEntry,
  makeSourceEnvelope,
  withClaudeBudget,
} from '../test-utils/sourceEnvelope';
import type {
  ClaudeBudgetDomain,
  CodexSourceData,
  Envelope,
  ProviderBudgetStatus,
  SourceEntry,
  SourcesMap,
} from '../types/envelope';

// #556 S5 §4 — the configured-BUDGET block, across its states and its three
// hosts. The fixtures are deliberately UNEQUAL between providers: Claude runs a
// `subscription-week` budget of $250 at a `warn` verdict, Codex a
// `calendar-month` budget of $100 at `ok`. A section that read the wrong
// provider would go red on any of five fields rather than pass green.

function envWith(opts: {
  claudeBudget?: Partial<ClaudeBudgetDomain>;
  codexData?: CodexSourceData;
}): Envelope {
  const claudeData = opts.claudeBudget == null
    ? makeClaudeSourceData()
    : withClaudeBudget(makeClaudeSourceData(), opts.claudeBudget);
  const claude = makeClaudeSourceEntry({ data: claudeData });
  const codex = opts.codexData == null
    ? makeCodexSourceEntry()
    : makeCodexSourceEntry({ data: opts.codexData });
  return makeSourceEnvelope({
    sources: {
      claude,
      codex,
      all: makeAllSourceEntry(claude, codex),
    } as unknown as SourcesMap,
  }) as unknown as Envelope;
}

function configuredClaudeEnv(over: Partial<ProviderBudgetStatus> = {}): Envelope {
  return envWith({ claudeBudget: { status: { ...makeClaudeBudgetStatus(), ...over } } });
}

beforeEach(() => {
  _resetForTests();
  cleanup();
});

describe('presentationBudgetComposition (#556 S5 §4.1)', () => {
  it('emits one section for a provider tab and two, Claude first, for All', () => {
    const env = configuredClaudeEnv();
    expect(presentationBudgetComposition(env, 'claude').sections.map((s) => s.source))
      .toEqual(['claude']);
    expect(presentationBudgetComposition(env, 'codex').sections.map((s) => s.source))
      .toEqual(['codex']);
    expect(presentationBudgetComposition(env, 'all').sections.map((s) => s.source))
      .toEqual(['claude', 'codex']);
  });

  it('reads a configured status', () => {
    const sections = presentationBudgetComposition(configuredClaudeEnv(), 'all').sections;
    const claude = sections.find((s) => s.source === 'claude')!;
    expect(claude.value).toEqual({ state: 'configured', status: makeClaudeBudgetStatus() });
    // Value-distinctness: Codex is a DIFFERENT period and amount, so a section
    // that read the wrong provider cannot pass.
    const codex = sections.find((s) => s.source === 'codex')!;
    expect(codex.value).toMatchObject({
      state: 'configured',
      status: { period: 'calendar-month', budget_usd: 100 },
    });
  });

  it('maps an ABSENT Claude status key and a NULL Codex status to the same state', () => {
    // The one documented wire asymmetry (§3.3): Claude omits the key, Codex
    // emits `"status": null`. Both are `provider_budget_unset` to the client.
    const codexData = makeCodexSourceData();
    const env = envWith({
      codexData: { ...codexData, budget: { ...codexData.budget, status: null } },
    });
    const sections = presentationBudgetComposition(env, 'all').sections;
    const claude = sections.find((s) => s.source === 'claude')!.value;
    const codex = sections.find((s) => s.source === 'codex')!.value;
    expect(claude).toEqual({ state: 'not_configured', disposition: 'provider_budget_unset' });
    expect(codex).toEqual(claude);
  });

  it('names the account_budgets_only disposition rather than collapsing it', () => {
    const env = envWith({
      claudeBudget: { not_configured: { disposition: 'account_budgets_only' } },
    });
    expect(presentationBudgetComposition(env, 'claude').sections[0].value)
      .toEqual({ state: 'not_configured', disposition: 'account_budgets_only' });
  });

  // #556 S5 Unit 2 review F1 — the Codex half of the same disposition. Codex
  // emits `status: null` ALONGSIDE `not_configured`, unlike Claude which omits
  // `status` entirely, so a reader that checked only for an absent key would
  // collapse this back into `provider_budget_unset` and render "No budget set."
  // to a user with per-account Codex budgets configured.
  it('reads account_budgets_only from Codex, whose status stays null beside it', () => {
    const codexData = makeCodexSourceData();
    const env = envWith({
      codexData: {
        ...codexData,
        budget: {
          ...codexData.budget,
          status: null,
          not_configured: { disposition: 'account_budgets_only' },
        },
      },
    });
    expect(presentationBudgetComposition(env, 'codex').sections[0].value)
      .toEqual({ state: 'not_configured', disposition: 'account_budgets_only' });
  });

  it('names each unavailable reason and keeps the whole server object', () => {
    for (const code of ['period_unresolved', 'budget_compute_failed']) {
      const unavailable = {
        code, message: 'nope', provider: 'claude' as const,
        budget_usd: 250, period: 'calendar-month',
      };
      const env = envWith({ claudeBudget: { status_unavailable: unavailable } });
      expect(presentationBudgetComposition(env, 'claude').sections[0].value)
        .toEqual({ state: 'unavailable', reason: code, unavailable });
    }
  });

  it('passes through a disposition and a reason this client has never seen', () => {
    // Closed for the server, OPEN for the client (S2's rule).
    const disp = envWith({
      claudeBudget: { not_configured: { disposition: 'invented_by_a_newer_server' } },
    });
    expect(presentationBudgetComposition(disp, 'claude').sections[0].value)
      .toEqual({ state: 'not_configured', disposition: 'invented_by_a_newer_server' });
    const unavailable = {
      code: 'invented_reason', message: 'x', provider: 'claude' as const,
    };
    const reason = envWith({ claudeBudget: { status_unavailable: unavailable } });
    expect(presentationBudgetComposition(reason, 'claude').sections[0].value)
      .toEqual({ state: 'unavailable', reason: 'invented_reason', unavailable });
  });
});

describe('BudgetComposition rendering (#556 S5 §4.4/§4.6)', () => {
  it('renders the provider figures, period, verdict and pace on a provider tab', () => {
    render(<BudgetComposition env={configuredClaudeEnv()} selection="claude" surface="panel" />);
    expect(screen.getByTestId('budget-spend-claude').textContent).toBe('$212.75');
    expect(screen.getByTestId('budget-target-claude').textContent).toBe('$250');
    expect(screen.getByTestId('budget-period-claude').textContent).toBe('subscription-week');
    expect(screen.getByTestId('budget-consumption-claude').textContent).toBe('85%');
    expect(screen.getByTestId('budget-verdict-claude').textContent).toBe('warn');
    expect(screen.getByTestId('budget-pace-claude').textContent).toBe('$30.39 / day');
  });

  it('renders two regions under All and preserves each provider period', () => {
    const { container } = render(
      <BudgetComposition env={configuredClaudeEnv()} selection="all" surface="panel" />,
    );
    const sections = container.querySelectorAll('[data-budget-section]');
    expect(Array.from(sections).map((s) => s.getAttribute('data-budget-section')))
      .toEqual(['claude', 'codex']);
    expect(screen.getByTestId('budget-period-claude').textContent).toBe('subscription-week');
    expect(screen.getByTestId('budget-period-codex').textContent).toBe('calendar-month');
    // Nothing is composed: there is no third, combined budget figure.
    expect(container.querySelectorAll('[data-budget-section]').length).toBe(2);
  });

  it('gives every section a region role and its own surface-qualified heading id', () => {
    const { container } = render(
      <>
        <BudgetComposition env={configuredClaudeEnv()} selection="all" surface="panel" />
        <BudgetComposition env={configuredClaudeEnv()} selection="all" surface="modal" />
      </>,
    );
    const ids = Array.from(container.querySelectorAll('[data-budget-section]'))
      .map((s) => {
        expect(s.getAttribute('role')).toBe('region');
        return s.getAttribute('aria-labelledby');
      });
    expect(ids).toEqual([
      'budget-panel-claude-heading',
      'budget-panel-codex-heading',
      'budget-modal-claude-heading',
      'budget-modal-codex-heading',
    ]);
    // Panel and modal can be mounted together, so the ids must stay unique.
    expect(new Set(ids).size).toBe(4);
    for (const id of ids) {
      expect(container.querySelectorAll(`#${id}`).length).toBe(1);
    }
  });

  it('renders a configured budget with ZERO spend as data, not as an empty state', () => {
    const env = configuredClaudeEnv({
      spent_usd: 0, consumption_pct: 0, remaining_usd: 250,
      verdict: 'ok', pace: { daily_usd: 0, projected_low_usd: 0, projected_high_usd: 0, week_avg_projection_usd: 0 },
    });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.getByTestId('budget-spend-claude').textContent).toBe('$0');
    expect(screen.getByTestId('budget-consumption-claude').textContent).toBe('0%');
    expect(screen.queryByTestId('budget-empty-claude')).toBeNull();
    expect(screen.queryByText('No budget set.')).toBeNull();
  });

  it('qualifies a low-confidence budget without hiding any of its figures', () => {
    const env = configuredClaudeEnv({ low_confidence: true });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.getByTestId('budget-lowconf-claude').textContent).toBe('low confidence');
    expect(screen.getByTestId('budget-spend-claude').textContent).toBe('$212.75');
    expect(screen.getByTestId('budget-verdict-claude').textContent).toBe('warn');
  });

  it('renders the provider-correct set command for provider_budget_unset', () => {
    const codexData = makeCodexSourceData();
    const env = envWith({
      codexData: { ...codexData, budget: { ...codexData.budget, status: null } },
    });
    const { container } = render(
      <BudgetComposition env={env} selection="all" surface="panel" />,
    );
    const claude = container.querySelector('[data-budget-section="claude"]') as HTMLElement;
    const codex = container.querySelector('[data-budget-section="codex"]') as HTMLElement;
    expect(within(claude).getByText('No budget set.')).toBeTruthy();
    expect(within(claude).getByText('cctally budget set <amount>')).toBeTruthy();
    expect(within(codex).getByText('No budget set.')).toBeTruthy();
    expect(within(codex).getByText('cctally budget set <amount> --vendor codex')).toBeTruthy();
  });

  it('renders account_budgets_only with its OWN copy, never "No budget set."', () => {
    // Telling a user with per-account budgets that they have none is a lie.
    const env = envWith({
      claudeBudget: { not_configured: { disposition: 'account_budgets_only' } },
    });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.getByText('No provider-wide budget set.')).toBeTruthy();
    expect(screen.queryByText('No budget set.')).toBeNull();
    expect(screen.queryByText('cctally budget set <amount>')).toBeNull();
  });

  it('renders each unavailable reason and never a fabricated percent or verdict', () => {
    const env = envWith({
      claudeBudget: {
        status_unavailable: {
          code: 'period_unresolved', message: 'x', provider: 'claude',
        },
      },
    });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.getByTestId('budget-unavailable-claude')).toBeTruthy();
    expect(screen.getByTestId('budget-reason-claude').textContent).toBe('period_unresolved');
    expect(screen.queryByTestId('budget-consumption-claude')).toBeNull();
    expect(screen.queryByTestId('budget-verdict-claude')).toBeNull();
    // Distinguishable from every unconfigured state.
    expect(screen.queryByText('No budget set.')).toBeNull();
    expect(screen.queryByText('No provider-wide budget set.')).toBeNull();
  });

  // #556 S5 §4.6 / Unit 2 review F5. §4.6 requires this state to render "the
  // configured amount with the window named as unresolved", and the block used
  // to print only the raw code. The amount is deliberately UNEQUAL to the
  // configured-status fixture's $250, so an implementation that fell back to
  // the configured status object cannot pass.
  it('states the configured amount, period and server message when unresolved', () => {
    const env = envWith({
      claudeBudget: {
        status_unavailable: {
          code: 'period_unresolved',
          message: "Claude's budget period could not be resolved.",
          provider: 'claude',
          budget_usd: 175.5,
          period: 'calendar-month',
        },
      },
    });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.getByTestId('budget-target-claude').textContent).toBe('$175.50');
    expect(screen.getByTestId('budget-period-claude').textContent).toBe('calendar-month');
    expect(screen.getByTestId('budget-window-claude').textContent).toBe('window unresolved');
    expect(screen.getByTestId('budget-message-claude').textContent)
      .toBe("Claude's budget period could not be resolved.");
    // Still no fabricated spend, percent or verdict.
    expect(screen.queryByTestId('budget-spend-claude')).toBeNull();
    expect(screen.queryByTestId('budget-consumption-claude')).toBeNull();
    expect(screen.queryByTestId('budget-verdict-claude')).toBeNull();
  });

  // #556 S5 browser-QA P2. The browser gate found this card rendering its only
  // figure at 13.5px/600/`--text-dim` — the treatment the configured card gives
  // the right-hand half of "spend of target" — so beside a configured card it
  // stated no prominent number and the amber qualifier was its most salient
  // element. The qualifier also has to leave the figures row: with four items
  // that row wrapped and pushed the period chip onto a line of its own.
  it('gives the lone configured figure headline prominence, qualifier on its own line', () => {
    const env = envWith({
      claudeBudget: {
        status_unavailable: {
          code: 'period_unresolved',
          message: 'x',
          provider: 'claude',
          budget_usd: 180,
          period: 'subscription-week',
        },
      },
    });
    const { container } = render(
      <BudgetComposition env={env} selection="claude" surface="panel" />,
    );
    const target = screen.getByTestId('budget-target-claude');
    expect(target.className).toContain('is-lone');
    const figures = container.querySelector('.budget-figures');
    expect(figures).not.toBeNull();
    // The figure and the period chip share the row; the qualifier does not.
    expect(figures!.contains(target)).toBe(true);
    expect(figures!.contains(screen.getByTestId('budget-period-claude'))).toBe(true);
    expect(figures!.contains(screen.getByTestId('budget-window-claude'))).toBe(false);
  });

  it('omits the amount row entirely when the server sent no amount', () => {
    const env = envWith({
      claudeBudget: {
        status_unavailable: {
          code: 'budget_compute_failed', message: 'x', provider: 'claude',
        },
      },
    });
    render(<BudgetComposition env={env} selection="claude" surface="panel" />);
    expect(screen.queryByTestId('budget-target-claude')).toBeNull();
    expect(screen.queryByTestId('budget-window-claude')).toBeNull();
    expect(screen.getByTestId('budget-reason-claude').textContent)
      .toBe('budget_compute_failed');
  });

  // #556 S5 Unit 2 review F1 — the rendered half of the Codex disposition.
  it('renders the account-only copy for a per-account-only Codex budget', () => {
    const codexData = makeCodexSourceData();
    const env = envWith({
      codexData: {
        ...codexData,
        budget: {
          ...codexData.budget,
          status: null,
          not_configured: { disposition: 'account_budgets_only' },
        },
      },
    });
    render(<BudgetComposition env={env} selection="codex" surface="panel" />);
    expect(screen.getByText('No provider-wide budget set.')).toBeTruthy();
    expect(screen.queryByText('No budget set.')).toBeNull();
    expect(screen.queryByText('cctally budget set <amount> --vendor codex')).toBeNull();
  });

  it('falls back to generic copy for an unrecognised disposition and reason', () => {
    const disp = envWith({
      claudeBudget: { not_configured: { disposition: 'from_the_future' } },
    });
    const { unmount } = render(
      <BudgetComposition env={disp} selection="claude" surface="panel" />,
    );
    expect(screen.getByText('No budget status to show.')).toBeTruthy();
    expect(screen.queryByText('No budget set.')).toBeNull();
    unmount();
    const reason = envWith({
      claudeBudget: {
        status_unavailable: { code: 'from_the_future', message: 'x', provider: 'claude' },
      },
    });
    render(<BudgetComposition env={reason} selection="claude" surface="panel" />);
    expect(screen.getByText('Budget status is unavailable.')).toBeTruthy();
  });

  it('adds remaining, the projection band, the 24h rate, thresholds and bounds in the modal', () => {
    render(<BudgetComposition env={configuredClaudeEnv()} selection="claude" surface="modal" />);
    expect(screen.getByTestId('budget-projection-claude').textContent)
      .toBe('$240.00 – $268.00');
    expect(screen.getByTestId('budget-thresholds-claude').textContent).toBe('80% · 95%');
    expect(screen.getByTestId('budget-window-claude').textContent).toContain('→');
    expect(screen.getByText('Remaining')).toBeTruthy();
    expect(screen.getByText('Recent-24h')).toBeTruthy();
  });

  it('degrades to the section reason when the provider has no data at all', () => {
    const claude = makeClaudeSourceEntry({ data: null } as unknown as
      Partial<SourceEntry<never>>);
    const env = makeSourceEnvelope({
      sources: {
        claude,
        codex: makeCodexSourceEntry(),
        all: makeAllSourceEntry(claude, makeCodexSourceEntry()),
      } as unknown as SourcesMap,
    }) as unknown as Envelope;
    const { container } = render(
      <BudgetComposition env={env} selection="claude" surface="panel" />,
    );
    expect(container.querySelector('.provider-section-reason')?.textContent)
      .toContain('Claude budget is unavailable.');
  });
});
