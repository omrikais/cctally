import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';
import { Modal } from './Modal';
import { ZoneTag } from '../components/ZoneTag';
import { getState, subscribeStore } from '../store/store';
import { useDiagnosis } from '../hooks/useDiagnosis';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { fmt } from '../lib/fmt';
import {
  classLabel,
  diagnosisWithheldMessage,
  evidenceLabel,
  evidenceValue,
  evidenceWithheldMessage,
  gapCodeMessage,
  publishesGaps,
  reachesContributorFloor,
  registryEntry,
  supportShortfallMessage,
  transcriptAsymmetryPresent,
  TRANSCRIPT_ASYMMETRY_CLASS,
  TRANSCRIPT_ASYMMETRY_NOTE,
  type DiagnosisClass,
  type DiagnosisConstants,
  type DiagnosisReport,
  type DiagnosisResult,
  type DiagnosisRow,
  type EvidenceField,
  type PopulationCoverage,
} from '../lib/diagnosis';

// The dialog's aria-labelledby target. Named once because the retry's
// focus recovery looks the element up by this id.
const TITLE_ID = 'explain-modal-title';

// #620 S2 — the dashboard's answer to "where did this window's money go".
//
// It reads `GET /api/diagnosis`, which publishes exactly what the CLI's
// `--json` publishes, through the same adapter. Nothing here recomputes a
// figure the server measured; the only rule this file applies is the published
// contributor-floor rule, and it applies it with the published constants.
//
// Three things it must never do, each of which the contract states outright:
//
//   * render a withheld figure as a number. `$0.00` beside four classes
//     withheld as `pricing_unavailable` states a retained cost that is unknown,
//     not zero.
//   * render an absent coverage dimension as `0`. Absence is "not measured";
//     zero is a measurement.
//   * switch exhaustively over a cause. Every cause code is a bare string with
//     a required fallback branch, because an old client meets a newer server on
//     the in-place update path.
//
// It is a list rather than a table on purpose: at 390px a table header scrolls
// out of view and orphans its values from their column names (R8). Each row
// states its own labels.

interface Ctx {
  tz: string;
  offsetLabel: string;
}

function instant(iso: string | null | undefined, ctx: Ctx): string {
  return iso ? fmt.datetimeShort(iso, ctx) : '—';
}

/** A dollar figure that refuses to invent one. */
function EvidenceUsd({ field }: { field: EvidenceField }): JSX.Element {
  if (field.state !== 'available' || typeof field.value !== 'number') {
    return (
      <span className="explain-withheld">
        {diagnosisWithheldMessage(field.code)}
        {field.qualifications.length > 0 ? ` ${field.qualifications.join(' ')}` : ''}
      </span>
    );
  }
  return <span className="explain-usd">{fmt.usd2(field.value)}</span>;
}

function sharePercent(field: EvidenceField): string | null {
  if (field.state !== 'available' || typeof field.value !== 'number') return null;
  return `${(field.value * 100).toFixed(1)}%`;
}

function CoverageNote({
  population, confidence, contributorClass, constants, excludeGapCodes = [],
}: {
  population: PopulationCoverage | null;
  // Rendered as the last part, so a class line and a contributor row state the
  // same facts in the same order as the terminal. Absent for a class that
  // measured nothing: confidence is a statement about a measurement.
  confidence?: string | null;
  // Which class this population belongs to. It decides whether the gap line is
  // published at all — see `publishesGaps`.
  contributorClass: string;
  constants: DiagnosisConstants;
  // The codes this line has already stated elsewhere. A class withheld for a
  // cause carries that same cause as its first gap code, so without this the
  // surface states it twice: once as the class's cause in English, once as an
  // undecided reason a line later.
  excludeGapCodes?: readonly string[];
}): JSX.Element | null {
  if (population == null) return null;
  const parts: string[] = [];
  // `supportUnits: null` means the class never shaped a population, which is
  // not the same as shaping one and finding it empty.
  parts.push(population.supportUnits == null
    ? 'support not measured'
    : `support ${population.supportUnits} ${population.supportUnits === 1 ? 'unit' : 'units'}`);
  // Each dimension is rendered ONLY when present. An absent dimension is not
  // zero, and printing `0%` for one would assert a measurement nobody made.
  if (population.usdCoverage != null) {
    parts.push(`cost coverage ${(population.usdCoverage * 100).toFixed(0)}%`);
  }
  if (population.identityCoverage != null) {
    parts.push(`identity coverage ${(population.identityCoverage * 100).toFixed(0)}%`);
  }
  if (population.pricingCoverage != null) {
    parts.push(`priced without fallback ${(population.pricingCoverage * 100).toFixed(0)}%`);
  }
  if (population.retentionCoverage != null) {
    parts.push(`window retained ${(population.retentionCoverage * 100).toFixed(0)}%`);
  }
  // #620 S3. The only dimension that falls when a row could not be DECIDED,
  // which is a different statement from a row whose identity did not resolve.
  if (population.evaluabilityCoverage != null) {
    parts.push(`decidable ${(population.evaluabilityCoverage * 100).toFixed(0)}%`);
  }
  if (confidence != null && confidence !== '') parts.push(`confidence ${confidence}`);
  // Why rows could not be decided, in words. Each code goes through a mapper
  // with a required fallback branch, because the vocabulary is closed on the
  // server and open here — a code from a newer server must say something
  // honest rather than disappear.
  //
  // The same two guards the terminal applies, so the two surfaces state the
  // same set of gap codes: only a class that can fail to decide a row
  // publishes the line at all, and a code the surrounding text has already
  // stated is dropped rather than repeated in different words.
  const excluded = new Set(excludeGapCodes);
  const gaps = (publishesGaps(contributorClass, constants)
    ? (population.gapCodes ?? []) : [])
    .filter((code) => !excluded.has(code))
    .map((code) => gapCodeMessage(code))
    .filter((message) => message !== '');
  return (
    <>
      <p className="explain-coverage">{parts.join(' · ')}</p>
      {/* One `undecided:` lead-in over the whole list, which is how the
          terminal states the same fact (`undecided: unknown_context_window`).
          Written here rather than inside each branch of `gapCodeMessage`, so
          a second gap code does not repeat the word — and so the recognised
          codes cannot be the only ones missing it. The line sits directly
          under the coverage line at the same size and family, and the lead-in
          is the only non-colour cue that separates them. */}
      {gaps.length > 0 ? (
        <p className="explain-coverage explain-gaps">
          undecided: {gaps.join(' · ')}
        </p>
      ) : null}
    </>
  );
}

/** The class-specific evidence, VISIBLE beneath the row rather than behind a
 *  `title` attribute, which a touch device never reveals (R9). A withheld
 *  member prints its cause: rendering nothing there would read as zero. */
function EvidenceFigures({ evidence, stated }: {
  evidence: Record<string, EvidenceField> | undefined;
  // The qualifications the row has ALREADY stated, in `.explain-row-meta`.
  // A qualification repeated under a figure reads as a second, separate fact
  // about that figure — the browser gate read
  // `identifiable_subset_unknown_completeness` twice in one row, with the
  // rule sentence beneath both stating the same thing in English.
  stated: readonly string[];
}): JSX.Element | null {
  const names = Object.keys(evidence ?? {}).sort();
  if (names.length === 0) return null;
  const already = new Set(stated);
  return (
    <p className="explain-row-figures explain-evidence">
      {names.map((name) => {
        const field = (evidence ?? {})[name];
        const value = evidenceValue(name, field);
        const marks = (field?.qualifications ?? [])
          .filter((mark) => !already.has(mark));
        return (
          <span className="explain-figure" key={name} data-evidence={name}>
            <span className="explain-figure-label">{evidenceLabel(name)}</span>
            {value == null ? (
              // The FIELD's vocabulary, not the class's: this figure was not
              // established while its class was, and the class sentence
              // ("too little in this window to measure this class")
              // contradicts the cost, share and sibling figures on the row
              // it sits in.
              <span className="explain-withheld">
                {evidenceWithheldMessage(field?.code)}
              </span>
            ) : (
              <span className="explain-usd">{value}</span>
            )}
            {marks.length > 0 ? (
              <span className="explain-evidence-qualification">
                {marks.join(' · ')}
              </span>
            ) : null}
          </span>
        );
      })}
    </p>
  );
}

/** The published predicate of one class, taken from the registry the server
 *  publishes rather than a client-side copy — a second copy would state the
 *  old rule the moment a threshold moved. */
function RuleLine({ contributorClass, constants }: {
  contributorClass: string; constants: DiagnosisConstants;
}): JSX.Element | null {
  const rule = registryEntry(contributorClass, constants)?.rule;
  if (rule == null) return null;
  return <p className="explain-rule">{rule.sentence}</p>;
}

function ContributorRow({
  row, constants,
}: { row: DiagnosisRow; constants: DiagnosisConstants }): JSX.Element {
  const share = sharePercent(row.share);
  const above = reachesContributorFloor(
    typeof row.share.value === 'number' ? row.share.value : null, constants,
  );
  const baseline = row.baseline.state === 'available'
    && typeof row.baseline.value === 'number'
    ? `${(row.baseline.value * 100).toFixed(1)}% in the previous window`
    : diagnosisWithheldMessage(row.baseline.code);
  return (
    <li className="explain-row" data-class={row.contributorClass}>
      <p className="explain-row-head">
        <span className="explain-rank">{row.rank == null ? '—' : `#${row.rank}`}</span>
        <span className="explain-row-class">
          {classLabel(row.contributorClass, constants)}
        </span>
        <span className="explain-row-subject">{row.subjectLabel}</span>
        {above ? (
          <span className="chip explain-chip-contributor">contributor</span>
        ) : null}
      </p>
      <p className="explain-row-figures">
        <span className="explain-figure">
          <span className="explain-figure-label">observed cost</span>
          <EvidenceUsd field={row.observedUsd} />
        </span>
        <span className="explain-figure">
          <span className="explain-figure-label">share of the denominator</span>
          <span className="explain-share">
            {share ?? diagnosisWithheldMessage(row.share.code)}
          </span>
        </span>
        <span className="explain-figure">
          <span className="explain-figure-label">against the previous window</span>
          <span className="explain-baseline">{baseline}</span>
        </span>
      </p>
      <p className="explain-row-meta">
        confidence {row.confidence}
        {row.isFallbackPricing ? ' · priced through a fallback rate' : ''}
        {row.observedUsd.qualifications.map((q) => ` · ${q}`).join('')}
      </p>
      {/* The population these figures rest on. Every class line below states
          its own through the same component; a contributor row that stated
          none was the one figure on this surface a reader could not weigh.
          The three evidence fields carry the same class population, so the
          observed cost — the figure the row is ranked by — is the one read. */}
      <CoverageNote population={row.observedUsd.population}
                    contributorClass={row.contributorClass}
                    constants={constants} />
      <EvidenceFigures evidence={row.evidence}
                       stated={row.observedUsd.qualifications} />
      <RuleLine contributorClass={row.contributorClass} constants={constants} />
      <p className="explain-row-next">
        <span className="explain-figure-label">next step</span>
        <code>{row.nextStep}</code>
      </p>
    </li>
  );
}

function ClassLine({
  entry, constants, withheld, asymmetryPresent,
}: {
  entry: DiagnosisClass;
  constants: DiagnosisConstants;
  withheld: boolean;
  // Whether another source in this report measured the class the transcript
  // asymmetry note is about. Decided once per provider section, because it is
  // a fact about the whole report rather than about this line.
  asymmetryPresent: boolean;
}): JSX.Element {
  const shortfall = withheld
    ? supportShortfallMessage(entry.supportShortfall,
                              registryEntry(entry.contributorClass, constants))
    : '';
  // The note explains a contrast, so it is stated only where both halves of
  // one exist: this class denied for want of transcript access, and the same
  // class measured on a provider that needs none. Under any other class, or
  // on a report with no such provider in it, it describes nothing on screen.
  const statesAsymmetry = withheld
    && entry.contributorClass === TRANSCRIPT_ASYMMETRY_CLASS
    && entry.code === 'transcripts_not_visible'
    && asymmetryPresent;
  return (
    <li className="explain-class" data-class={entry.contributorClass}
        data-verdict={entry.verdict}>
      <p className="explain-class-head">
        {classLabel(entry.contributorClass, constants)}
      </p>
      {withheld ? (
        <p className="explain-withheld">
          {diagnosisWithheldMessage(entry.code)}
          {shortfall ? ` (${shortfall})` : ''}
        </p>
      ) : null}
      {statesAsymmetry ? (
        <p className="explain-withheld explain-asymmetry">
          {TRANSCRIPT_ASYMMETRY_NOTE}
        </p>
      ) : null}
      {/* A withheld class states NO confidence: confidence is a statement
          about a measurement, and it made none — and the server publishes
          `null` there, so this surface never has to decide it. `CoverageNote`
          prints the population either way, because that much was measured. */}
      <CoverageNote population={entry.population}
                    confidence={withheld ? null : entry.confidence}
                    contributorClass={entry.contributorClass}
                    constants={constants}
                    excludeGapCodes={entry.code ? [entry.code] : []} />
      <RuleLine contributorClass={entry.contributorClass}
                constants={constants} />
    </li>
  );
}

function ProviderSection({
  result, constants, asymmetryPresent,
}: {
  result: DiagnosisResult;
  constants: DiagnosisConstants;
  asymmetryPresent: boolean;
}): JSX.Element {
  const clean = result.classes.filter((c) => c.verdict === 'no_contributor');
  const withheld = result.classes.filter((c) => c.verdict === 'withheld');
  const inapplicable = result.classes.filter((c) => c.verdict === 'not_applicable');
  return (
    <section className="explain-provider" data-source={result.source}>
      <h3>
        {result.source} · {result.accountKey ?? 'all accounts'}
        {result.effectiveSpeed ? ` · ${result.effectiveSpeed}` : ''}
      </h3>
      {/* `identity` is the denominator's stable identity on the wire, so its
          camelCase is not ours to change — but `.explain-figure-label`
          uppercases, and those word boundaries were the identifier's only
          readability cue, so it painted `TOTALEXPLAINEDRETAINEDCOST`. The
          label carries the word the CLI uses and the identifier sits in its
          own element, which uppercases nothing. */}
      <p className="explain-denominator">
        <span className="explain-figure-label">Denominator</span>
        <span className="explain-denominator-identity">
          {result.denominator.identity}
        </span>
        <EvidenceUsd field={result.denominator.usd} />
        {result.denominator.usd.state === 'available'
          ? <span className="explain-denominator-note">of locally retained cost</span>
          : null}
      </p>

      {result.contributors.length > 0 ? (
        <>
          <h4>Reported contributors, ranked by observed cost</h4>
          <ul className="explain-rows">
            {result.contributors.map((row) => (
              <ContributorRow
                key={`${row.contributorClass}:${row.subjectKey}`}
                row={row}
                constants={constants}
              />
            ))}
          </ul>
        </>
      ) : null}

      {clean.length > 0 ? (
        <>
          <h4>Classes reporting no contributor</h4>
          <ul className="explain-classes">
            {clean.map((entry) => (
              <ClassLine key={entry.contributorClass} entry={entry}
                         constants={constants} withheld={false}
                         asymmetryPresent={asymmetryPresent} />
            ))}
          </ul>
        </>
      ) : null}

      {withheld.length > 0 ? (
        <>
          <h4>Classes with no measurement to report</h4>
          <ul className="explain-classes">
            {withheld.map((entry) => (
              <ClassLine key={entry.contributorClass} entry={entry}
                         constants={constants} withheld
                         asymmetryPresent={asymmetryPresent} />
            ))}
          </ul>
        </>
      ) : null}

      {/* `not_applicable` is NOT a withheld measurement — it says the provider
          structurally cannot support the class, and it is excluded from the
          completeness test for that reason. Filing it under the withheld
          heading would contradict the distinction the contract rests on. */}
      {inapplicable.length > 0 ? (
        <>
          <h4>Classes this provider does not support</h4>
          <ul className="explain-classes">
            {inapplicable.map((entry) => (
              <li key={entry.contributorClass} className="explain-class">
                {classLabel(entry.contributorClass, constants)} — not applicable
                to {result.source}
              </li>
            ))}
          </ul>
        </>
      ) : null}

      <p className="explain-verdict" data-verdict={result.verdict}>
        Verdict for {result.source}: {result.verdict}
        {result.code ? ` (${result.code})` : ''}
        {' '}
        <span className="explain-completeness">
          [{result.withheldClassCount} of {result.applicableClassCount} applicable
          {' '}classes withheld]
        </span>
      </p>
    </section>
  );
}

function ReportBody({
  report, alertFiredAt, ctx,
}: { report: DiagnosisReport; alertFiredAt: string | null; ctx: Ctx }): JSX.Element {
  const floor = report.constants.contributorShareFloor;
  return (
    <div className="explain-body">
      <dl className="explain-instants">
        <div>
          <dt>Window</dt>
          {/* The zone is named, not left to the numeric offset the per-instant
              suffix carries: `+03` is shared by several zones, and a zone that
              is `+03` in August is `+02` in January, so a reader given only the
              offset cannot say which window was measured. The CLI states the
              same fact as `[Etc/UTC]`. The name is `ctx.tz`, the zone these
              bounds were actually converted into, which is the same value the
              server publishes as `window.tz` — both resolve `display.tz`
              through one resolver — and naming the rendering zone is the half
              that cannot be wrong. */}
          <dd>
            {instant(report.window.startAt, ctx)} to{' '}
            {instant(report.window.endAt, ctx)}
            {' '}<ZoneTag tz={ctx.tz} />
            {' '}<span className="explain-halfopen">
              (the start is included, the end is not)
            </span>
          </dd>
        </div>
        {/* D3 — following a persisted warning re-measures its window against
            LIVE data, so the surface states both instants rather than letting
            the reader assume the figures are as of the alert. */}
        {alertFiredAt ? (
          <div>
            <dt>Alert fired</dt>
            <dd>{instant(alertFiredAt, ctx)} <ZoneTag tz={ctx.tz} /></dd>
          </div>
        ) : null}
        <div>
          <dt>Measured</dt>
          <dd>{instant(report.measuredAt, ctx)} <ZoneTag tz={ctx.tz} /></dd>
        </div>
      </dl>

      <p className="explain-note">{report.notes.scope}</p>
      <p className="explain-note">{report.notes.overlap}</p>
      <p className="explain-note">
        A subject is reported as a contributor at a share of {floor.toFixed(2)} or
        more of its named denominator.
      </p>
      {/* The turn predicate the two turn-based classes are defined over.
          `conversation_sessions.msg_count` is a sidechain-INCLUSIVE physical
          count, so a reader shown a bare turn count would read the wrong
          number; the server publishes the definition and this states it. */}
      {report.constants.turnDefinition ? (
        <p className="explain-note">{report.constants.turnDefinition}</p>
      ) : null}

      {report.results.map((result) => (
        <ProviderSection key={result.source} result={result}
                         constants={report.constants}
                         asymmetryPresent={
                           transcriptAsymmetryPresent(report, result.source)} />
      ))}

      <p className="explain-overall" data-verdict={report.overallVerdict}>
        Overall: {report.overallVerdict}
        {report.overallCode ? ` (${report.overallCode})` : ''}
        {' '}
        <span className="explain-completeness">
          [{report.withheldClassCount} of {report.applicableClassCount} applicable
          {' '}classes withheld]
        </span>
      </p>
    </div>
  );
}

export function ExplainModal(): JSX.Element {
  // Bound at open, like every other panel modal: a source switch on the board
  // behind an open modal must not change what it is describing.
  const source = useSyncExternalStore(
    subscribeStore,
    () => getState().openModalSource ?? getState().activeSource,
  );
  const accountKey = useSyncExternalStore(
    subscribeStore, () => getState().openExplainAccountKey,
  );
  const windowStartAt = useSyncExternalStore(
    subscribeStore, () => getState().openExplainWindowStartAt,
  );
  const windowEndAt = useSyncExternalStore(
    subscribeStore, () => getState().openExplainWindowEndAt,
  );
  const alertFiredAt = useSyncExternalStore(
    subscribeStore, () => getState().openExplainAlertFiredAt,
  );
  const display = useDisplayTz();
  const ctx: Ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const { report, loading, error, retryable, retry } = useDiagnosis({
    source, accountKey, windowStartAt, windowEndAt,
  });

  // Activating the retry clears the error, which unmounts the error block and
  // the button inside it; when the request fails again the button returns as a
  // NEW element, and nothing was putting focus back on it. A keyboard user had
  // to Tab back to the control they had just pressed, which is also how the
  // browser gate came to record the retry as doing nothing.
  //
  // The flag is set by the click rather than inferred from `loading`, so the
  // button is focused only when a person asked for the request that produced
  // it. A callback ref does the focusing because it runs at the moment the new
  // element is committed, which is the one moment the old one is already gone.
  const restoreRetryFocus = useRef(false);
  const bindRetryButton = useCallback((node: HTMLButtonElement | null) => {
    if (node != null && restoreRetryFocus.current) {
      restoreRetryFocus.current = false;
      node.focus();
    }
  }, []);
  const handleRetry = useCallback(() => {
    restoreRetryFocus.current = true;
    retry();
  }, [retry]);
  // A retry that SUCCEEDS renders no button, so the request must not leave the
  // flag armed for some later error to consume and steal focus from whatever
  // the reader is on by then. This runs after the commit that would have
  // remounted the button, so it disarms only a request that settled without
  // one.
  //
  // That same settlement also removes the element the reader activated, which
  // drops focus to document.body. The Tab-trap in useModalFocus returns early
  // when the active element is outside the card, so from body the next Tab
  // leaves a dialog that is still aria-modal and reaches the page skip link.
  // Focus therefore moves to the heading, which is the element the modal
  // focuses on open and carries tabIndex={-1} for exactly this purpose. The
  // orphaned-focus check keeps it from pulling back a reader who moved on
  // while the request was in flight.
  useEffect(() => {
    if (loading || (error != null && retryable)) return;
    if (!restoreRetryFocus.current) return;
    restoreRetryFocus.current = false;
    const active = document.activeElement;
    if (active != null && active !== document.body) return;
    document.getElementById(TITLE_ID)?.focus();
  }, [loading, error, retryable]);

  return (
    <Modal
      title="Where the cost went"
      accentClass="accent-indigo"
      dataSource={source}
      rootTestId="explain-modal"
      titleId={TITLE_ID}
      wide
    >
      {loading && report == null ? (
        <p className="explain-status">Measuring this window…</p>
      ) : null}
      {error != null ? (
        <div className="explain-status explain-error">
          {retryable ? (
            <>
              {/* `generation_incoherent` means a component moved twice while it
                  was read — a store under active write rather than a broken
                  one. The same request a moment later normally succeeds, so it
                  is presented as a retryable state rather than as a failure. */}
              <p>
                The store changed while this window was being measured, so no
                single coherent answer could be published. Nothing is wrong
                with the data.
              </p>
              <button type="button" className="explain-retry"
                      ref={bindRetryButton} onClick={handleRetry}>
                Measure again
              </button>
            </>
          ) : (
            <p>The diagnosis could not be requested: {error}</p>
          )}
        </div>
      ) : null}
      {report != null ? (
        <ReportBody report={report} alertFiredAt={alertFiredAt} ctx={ctx} />
      ) : null}
    </Modal>
  );
}
