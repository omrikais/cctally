import { useSnapshot } from '../hooks/useSnapshot';
import { useDisplayTz } from '../hooks/useDisplayTz';
import { ProgressBar } from '../components/ProgressBar';
import { PanelGrip } from '../components/PanelGrip';
import { fmt } from '../lib/fmt';
import { dispatch } from '../store/store';

function formatHHMMSS(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Magnitude-based classifier for the 5-hour Δ-pp sub-line.
//
// The 7d-since-5h delta is monotonic non-negative within a window (usage %
// only increases), so a sign-based positive/negative classifier carries no
// useful signal here. Bucket by absolute magnitude instead so the color
// reflects burn-rate intensity — the most actionable read on this card.
function fiveHourDeltaCls(deltaPp: number | null): string {
  if (deltaPp == null) return 'text-dim';
  const v = Math.abs(deltaPp);
  if (v <= 1.0) return 'delta-low';      // ≤ 1pp — quiet hour, good
  if (v <= 3.0) return 'delta-moderate'; // ≤ 3pp — typical
  if (v <= 5.0) return 'delta-elevated'; // ≤ 5pp — heads up
  return 'delta-high';                   // > 5pp — heavy burn
}

// Elapsed seconds between env.generated_at and fhb.block_start_at, used
// for the Block kv sub-line. Anchored to env.generated_at (NOT Date.now)
// so the value stays consistent under CCTALLY_AS_OF / fixture pinning —
// same idiom as `_handle_get_session_detail` pinning to snap.generated_at.
function blockElapsedSec(
  generatedAt: string | null | undefined,
  blockStartAt: string | null | undefined,
): number | null {
  if (!generatedAt || !blockStartAt) return null;
  const now = new Date(generatedAt);
  const start = new Date(blockStartAt);
  if (isNaN(now.getTime()) || isNaN(start.getTime())) return null;
  return Math.floor((now.getTime() - start.getTime()) / 1000);
}

export function CurrentWeekPanel() {
  const env = useSnapshot();
  const display = useDisplayTz();
  const ctx = { tz: display.resolvedTz, offsetLabel: display.offsetLabel };
  const cw = env?.current_week ?? null;
  const freshness = cw?.freshness ?? null;
  const fhb = cw?.five_hour_block ?? null;
  const showChip = freshness !== null && freshness.label !== 'fresh';

  return (
    <section
      className="panel accent-green"
      id="panel-current-week"
      tabIndex={0}
      role="region"
      aria-label="Current Week panel"
      data-panel-kind="current-week"
      onClick={() => dispatch({ type: 'OPEN_MODAL', kind: 'current-week' })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          dispatch({ type: 'OPEN_MODAL', kind: 'current-week' });
        }
      }}
    >
      <div className="panel-header">
        <svg className="icon" style={{ color: 'var(--accent-green)' }}>
          <use href="/static/icons.svg#trending-up" />
        </svg>
        <h3 style={{ color: 'var(--accent-green)' }}>Current Week</h3>
        {showChip && freshness && (
          <span
            className={
              freshness.label === 'aging' ? 'chip chip-aging' : 'chip chip-stale'
            }
            data-freshness={freshness.label}
            title={`Captured ${freshness.captured_at}`}
          >
            as of {formatHHMMSS(freshness.captured_at) ?? freshness.captured_at}
            {' · '}
            {freshness.age_seconds}s ago
          </span>
        )}
        <PanelGrip />
      </div>
      <div className="panel-body">
        <div className="cw-body">
          <div className="cw-left">
            <div className="cw-big">
              <div className="label">Used (7d)</div>
              <div className="num">{fmt.pct1(cw?.used_pct)}</div>
              <ProgressBar percent={cw?.used_pct} cells={30} />
              <div className="progress-scale">
                <span>0%</span>
                <span>50%</span>
                <span>100%</span>
              </div>
            </div>
          </div>
          <div className="cw-right">
            <div className="cw-kv kv-spent">
              <svg className="icon">
                <use href="/static/icons.svg#dollar" />
              </svg>
              <div className="body">
                <div className="row1">
                  <span className="k">Spent</span>
                  <span className="v magenta">{fmt.usd2(cw?.spent_usd)}</span>
                </div>
                <div className="sub">
                  <span className="mute">$/1%:</span>{' '}
                  <span className="magenta">{fmt.usd2(cw?.dollar_per_pct)}</span>
                </div>
              </div>
            </div>
            <div className="cw-kv kv-reset">
              <svg className="icon">
                <use href="/static/icons.svg#refresh" />
              </svg>
              <div className="body">
                <div className="row1">
                  <span className="k">Reset</span>
                  <span className="v amber">{fmt.datetimeShort(cw?.reset_at_utc, ctx)}</span>
                </div>
                <div className="sub">
                  in <span>{fmt.ddhh(cw?.reset_in_sec)}</span>
                </div>
              </div>
            </div>
            <div className="cw-kv kv-five-hour">
              <svg className="icon">
                <use href="/static/icons.svg#activity" />
              </svg>
              <div className="body">
                <div className="row1">
                  <span className="k">5-hour</span>
                  <span className="v cyan">{fmt.pct1(cw?.five_hour_pct)}</span>
                </div>
                {fhb ? (
                  fhb.crossed_seven_day_reset ? (
                    <div className="sub cw-delta-row delta-elevated">⚡ reset</div>
                  ) : (
                    <div
                      className={`sub cw-delta-row ${fiveHourDeltaCls(fhb.seven_day_pct_delta_pp)}`}
                    >
                      Δ {fmt.pp(fhb.seven_day_pct_delta_pp)} this block
                    </div>
                  )
                ) : (
                  <div className="sub">
                    resets in <span>{fmt.hhmm(cw?.five_hour_resets_in_sec)}</span>
                  </div>
                )}
              </div>
            </div>
            {fhb && (
              <div className="cw-kv kv-block">
                <svg className="icon">
                  <use href="/static/icons.svg#clock" />
                </svg>
                <div className="body">
                  <div className="row1">
                    <span className="k">Block</span>
                    <span className="v">
                      {formatHHMMSS(fhb.block_start_at) ?? fhb.block_start_at}
                    </span>
                  </div>
                  <div className="sub">
                    {(() => {
                      const elapsed = blockElapsedSec(env?.generated_at, fhb.block_start_at);
                      if (elapsed == null) return <>started <span>—</span></>;
                      if (elapsed <= 0) return <>started <span>just now</span></>;
                      return (
                        <>
                          started <span>{fmt.elapsedHm(elapsed)}</span> ago
                        </>
                      );
                    })()}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className="panel-foot cw-foot">
        <svg className="icon">
          <use href="/static/icons.svg#clock" />
        </svg>
        Last snapshot: <span>{fmt.agoSec(cw?.last_snapshot_age_sec)}</span>
      </div>
    </section>
  );
}
