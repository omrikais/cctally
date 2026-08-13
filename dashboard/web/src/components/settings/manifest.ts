// #513 S2 §2.4 — the disposition of every configuration key.
//
// The requirement this file exists for: every key in `ALLOWED_CONFIG_KEYS` has
// an on-screen disposition. Not "is editable or is invisible" — four states,
// and the third is the one that used to be missing.
//
//   editable   an editor is rendered here
//   readOnly   the value is shown, with no editor (its editor is parked)
//   disclosed  no editor, but the row is rendered and names the exact command
//   cliOnly    owned by the CLI, with the reason and the exact command
//
// "Unsurfaced" previously meant invisible, which contradicted the requirement.
// `budget.alerts_enabled` is the case that makes it matter: budget alerts fire
// only when an amount exists AND that master is true, so Settings cannot state
// the reason and the remedy for "budget configured, alerts off" while the
// controlling key is hidden.
//
// The commands are pinned STRINGS, and a test pins them per key. Asserting
// merely that the text is non-empty would pass vacuously against a row that
// said "use the CLI" and nothing more.
//
// One leaf sits outside this universe: `cache_report.anomaly_threshold_pp` is
// endpoint-writable but deliberately not a `config set` key, and it is edited
// in the cache-report popover rather than in this overlay. It is declared
// separately below so the partition test can account for it explicitly rather
// than by silence.
import type { SectionId } from './registry';

export type Disposition = 'editable' | 'readOnly' | 'disclosed' | 'cliOnly';

export interface ManifestEntry {
  key: string;
  label: string;
  section: SectionId;
  disposition: Disposition;
  // The registry id of the control, for `editable` rows only.
  fieldId?: string;
  // Exactly what to run, or exactly what to edit. Rendered verbatim.
  command: string;
  // One line: why this is not editable here. Empty for `editable` rows.
  reason: string;
  // The shipped default, stated because no provenance signal exists and none
  // is invented (§5.7).
  defaultText: string;
  // The four leaves POST /api/settings accepts and deliberately does not
  // persist (#134, #143). The row says so.
  acceptedThenDiscarded?: true;
}

export const MAP_ONLY_LEAF = 'cache_report.anomaly_threshold_pp';

export const SETTINGS_MANIFEST: readonly ManifestEntry[] = [
  // --- Display & time ------------------------------------------------------
  {
    key: 'display.tz',
    label: 'Display timezone',
    section: 'display',
    disposition: 'editable',
    fieldId: 'display.tz',
    command: 'cctally config set display.tz America/New_York',
    reason: '',
    defaultText: 'local',
  },
  // --- Recent Sessions -----------------------------------------------------
  // The three view preferences are browser-local and have no config key, so
  // they do not appear here. This manifest is keyed by configuration key.
  // --- Alerts --------------------------------------------------------------
  {
    key: 'alerts.enabled',
    label: 'Enable threshold alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'alerts.enabled',
    command: 'cctally config set alerts.enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'alerts.projected_enabled',
    label: 'Projected weekly-% pace alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'alerts.projected_enabled',
    command: 'cctally config set alerts.projected_enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'alerts.notifier',
    label: 'Alert notifier',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'alerts.notifier',
    command: 'cctally config set alerts.notifier osascript',
    reason: '',
    defaultText: 'auto',
  },
  {
    key: 'alerts.command_template',
    label: 'Custom notifier command',
    section: 'alerts',
    disposition: 'cliOnly',
    command:
      'cctally config set alerts.command_template \'["notify-send","{title}","{body}"]\'',
    reason:
      'It runs a local command and routinely holds secrets, so it never reaches a browser — only the boolean "a command is configured" does.',
    defaultText: 'null',
  },
  {
    key: 'alerts.quota',
    label: 'Codex quota alert rules',
    section: 'alerts',
    disposition: 'cliOnly',
    command:
      'cctally config set alerts.quota \'{"enabled": true, "actual_thresholds": [90, 95], "projected_thresholds": [], "rules": []}\'',
    reason: 'A whole rules object, written as one value rather than field by field.',
    defaultText: '{"enabled": false, "actual_thresholds": [90, 95], "projected_thresholds": [], "rules": []}',
  },
  {
    key: 'budget.weekly_usd',
    label: 'Weekly budget',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'budget.weekly_usd',
    command: 'cctally budget set 200',
    reason: '',
    defaultText: 'null',
  },
  {
    key: 'budget.alerts_enabled',
    label: 'Budget alerts master switch',
    section: 'alerts',
    disposition: 'disclosed',
    command: 'cctally config set budget.alerts_enabled true',
    reason:
      'Budget alerts fire only when an amount is set AND this master is on. There is no editor for it here yet, so this row is how you can tell which of the two is stopping them.',
    defaultText: 'true',
  },
  {
    key: 'budget.alert_thresholds',
    label: 'Budget alert thresholds',
    section: 'alerts',
    disposition: 'readOnly',
    command: 'cctally config set budget.alert_thresholds 90,100',
    reason: 'Shown above as a summary; its editor is parked in issue #19.',
    defaultText: '90,100',
  },
  {
    key: 'budget.projected_enabled',
    label: 'Projected budget-$ pace alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'budget.projected_enabled',
    command: 'cctally config set budget.projected_enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'budget.project_alerts_enabled',
    label: 'Per-project budget alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'budget.project_alerts_enabled',
    command: 'cctally config set budget.project_alerts_enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'budget.period',
    label: 'Claude budget period',
    section: 'alerts',
    disposition: 'cliOnly',
    acceptedThenDiscarded: true,
    command: 'cctally config set budget.period calendar-month',
    reason:
      'The dashboard accepts this leaf and runs the same forward-only reconcile the CLI does, but never stores it.',
    defaultText: 'subscription-week',
  },
  {
    key: 'budget.projects',
    label: 'Per-project budgets',
    section: 'alerts',
    disposition: 'cliOnly',
    command: 'cctally config set budget.projects \'{"/Users/you/repos/cctally-dev": 50}\'',
    reason: 'A map of canonical git-root paths to amounts, written as one object.',
    defaultText: '{}',
  },
  {
    key: 'budget.accounts',
    label: 'Per-account Claude budgets',
    section: 'alerts',
    disposition: 'cliOnly',
    command: 'cctally config set budget.accounts \'{"work": 200}\'',
    reason:
      'Account references are resolved to immutable account keys at write time, which a browser cannot do.',
    defaultText: '{}',
  },
  {
    key: 'budget.codex',
    label: 'Codex budget block',
    section: 'alerts',
    disposition: 'cliOnly',
    command: 'cctally budget set 200 --vendor codex',
    reason:
      'The whole Codex budget object. Amounts are never invented from a browser.',
    defaultText: 'null',
  },
  {
    key: 'budget.codex.amount_usd',
    label: 'Codex budget amount',
    section: 'alerts',
    disposition: 'cliOnly',
    acceptedThenDiscarded: true,
    command: 'cctally budget set 200 --vendor codex',
    reason: 'Accepted by the dashboard and dropped: amounts stay CLI-only.',
    defaultText: 'null',
  },
  {
    key: 'budget.codex.period',
    label: 'Codex budget period',
    section: 'alerts',
    disposition: 'cliOnly',
    acceptedThenDiscarded: true,
    command: 'cctally config set budget.codex.period calendar-month',
    reason: 'Accepted by the dashboard and dropped, like every CLI-only Codex leaf.',
    defaultText: 'calendar-month',
  },
  {
    key: 'budget.codex.alert_thresholds',
    label: 'Codex budget alert thresholds',
    section: 'alerts',
    disposition: 'cliOnly',
    acceptedThenDiscarded: true,
    command: 'cctally config set budget.codex.alert_thresholds 90,100',
    reason: 'Accepted by the dashboard and dropped, like every CLI-only Codex leaf.',
    defaultText: '90,100',
  },
  {
    key: 'budget.codex.alerts_enabled',
    label: 'Codex budget alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'budget.codex.alerts_enabled',
    command: 'cctally config set budget.codex.alerts_enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'budget.codex.projected_enabled',
    label: 'Codex projected-pace alerts',
    section: 'alerts',
    disposition: 'editable',
    fieldId: 'budget.codex.projected_enabled',
    command: 'cctally config set budget.codex.projected_enabled true',
    reason: '',
    defaultText: 'false',
  },
  {
    key: 'budget.codex.accounts',
    label: 'Per-account Codex budgets',
    section: 'alerts',
    disposition: 'cliOnly',
    command: 'cctally config set budget.codex.accounts \'{"work": 150}\'',
    reason:
      'Account references are resolved to immutable account keys at write time, which a browser cannot do.',
    defaultText: '{}',
  },
  // --- Conversation viewer -------------------------------------------------
  {
    key: 'dashboard.cache_failure_markers',
    label: 'Show cache-failure markers',
    section: 'viewer',
    disposition: 'editable',
    fieldId: 'dashboard.cache_failure_markers',
    command: 'cctally config set dashboard.cache_failure_markers false',
    reason: '',
    defaultText: 'true',
  },
  {
    key: 'dashboard.live_tail',
    label: 'Live-tail new turns',
    section: 'viewer',
    disposition: 'editable',
    fieldId: 'dashboard.live_tail',
    command: 'cctally config set dashboard.live_tail false',
    reason: '',
    defaultText: 'true',
  },
  {
    key: 'conversation.retention_days',
    label: 'Transcript retention',
    section: 'viewer',
    disposition: 'cliOnly',
    command: 'cctally config set conversation.retention_days 90',
    reason:
      'It governs deletion from cache.db, so it is set deliberately from the CLI rather than from a browser.',
    defaultText: '90',
  },
  // --- Access & updates ----------------------------------------------------
  {
    key: 'dashboard.lan_auth',
    label: 'Require LAN access token',
    section: 'access',
    disposition: 'editable',
    fieldId: 'dashboard.lan_auth',
    command: 'cctally config set dashboard.lan_auth false',
    reason: '',
    defaultText: 'true',
  },
  {
    key: 'dashboard.bind',
    label: 'Dashboard bind address',
    section: 'access',
    disposition: 'cliOnly',
    command: 'cctally config set dashboard.bind lan',
    reason:
      'It applies only at server startup, so changing it from the running dashboard could not take effect.',
    defaultText: 'loopback',
  },
  {
    key: 'dashboard.expose_transcripts',
    label: 'Expose transcripts beyond loopback',
    section: 'access',
    disposition: 'cliOnly',
    command: 'cctally config set dashboard.expose_transcripts true',
    reason:
      'A privacy gate read at bind time. It is deliberately not live-mutable from the surface it would expose.',
    defaultText: 'false',
  },
  {
    key: 'update.channel',
    label: 'Update channel',
    section: 'access',
    disposition: 'editable',
    fieldId: 'update.channel',
    command: 'cctally config set update.channel beta',
    reason: '',
    defaultText: 'stable',
  },
  {
    key: 'update.check.enabled',
    label: 'Background update check',
    section: 'access',
    disposition: 'disclosed',
    command: 'cctally config set update.check.enabled false',
    reason:
      'The endpoint can write it, but no editor is rendered here; the CLI is the way to change it.',
    defaultText: 'true',
  },
  {
    key: 'update.check.ttl_hours',
    label: 'Update-check interval',
    section: 'access',
    disposition: 'disclosed',
    command: 'cctally config set update.check.ttl_hours 24',
    reason:
      'The endpoint can write it, but no editor is rendered here; the CLI is the way to change it.',
    defaultText: '24',
  },
  {
    key: 'telemetry.enabled',
    label: 'Anonymous install-count telemetry',
    section: 'access',
    disposition: 'cliOnly',
    command: 'cctally telemetry off',
    reason:
      'Deliberately not dashboard-mirrored, so opting out is a decision you make at the CLI.',
    defaultText: 'true',
  },
  // --- Managed from the CLI (the trailing catch-all) -----------------------
  {
    key: 'statusline.cctally_extensions',
    label: 'Status-line cctally segment',
    section: 'cli',
    disposition: 'cliOnly',
    command: 'cctally config set statusline.cctally_extensions false',
    reason: 'It shapes the Claude Code status line, which this dashboard does not render.',
    defaultText: 'true',
  },
  {
    key: 'statusline.cost_source',
    label: 'Status-line cost source',
    section: 'cli',
    disposition: 'cliOnly',
    command: 'cctally config set statusline.cost_source cctally',
    reason: 'It shapes the Claude Code status line, which this dashboard does not render.',
    defaultText: 'auto',
  },
  {
    key: 'statusline.usage_only',
    label: 'Status-line usage-only mode',
    section: 'cli',
    disposition: 'cliOnly',
    command: 'cctally config set statusline.usage_only true',
    reason: 'It shapes the Claude Code status line, which this dashboard does not render.',
    defaultText: 'false',
  },
  {
    key: 'statusline.visual_burn_rate',
    label: 'Status-line burn-rate indicator',
    section: 'cli',
    disposition: 'cliOnly',
    command: 'cctally config set statusline.visual_burn_rate emoji',
    reason: 'It shapes the Claude Code status line, which this dashboard does not render.',
    defaultText: 'off',
  },
  {
    key: 'storage.artifact_retention',
    label: 'Recovery-artifact retention policy',
    section: 'cli',
    disposition: 'cliOnly',
    command:
      'cctally config set storage.artifact_retention \'{"max_age_days": 30, "max_count_per_family": 20, "max_total_mib": 4096, "min_free_mib": 10240, "max_shape_examples": 8}\'',
    reason:
      'A destructive retention policy written as one whole object; cctally never falls back to a policy you did not write.',
    defaultText:
      '{"max_age_days": 30, "max_count_per_family": 20, "max_total_mib": 4096, "min_free_mib": 10240, "max_shape_examples": 8}',
  },
  {
    key: 'codex.hook.ingest_budget_seconds',
    label: 'Codex hook ingest budget',
    section: 'cli',
    disposition: 'cliOnly',
    command: 'cctally config set codex.hook.ingest_budget_seconds 5',
    reason:
      'A hook-path deadline read outside any browser session; it never applies to this dashboard.',
    defaultText: '5',
  },
];

const BY_KEY = new Map(SETTINGS_MANIFEST.map((entry) => [entry.key, entry]));

export function manifestEntry(key: string): ManifestEntry | undefined {
  return BY_KEY.get(key);
}

export function manifestForSection(section: SectionId): ManifestEntry[] {
  return SETTINGS_MANIFEST.filter((entry) => entry.section === section);
}

// The rows this overlay renders WITHOUT an editor, in manifest order. Every one
// of them is reachable by the filter and states its exact command.
export function disclosureRowsForSection(section: SectionId): ManifestEntry[] {
  return SETTINGS_MANIFEST.filter(
    (entry) => entry.section === section && entry.disposition !== 'editable',
  );
}
