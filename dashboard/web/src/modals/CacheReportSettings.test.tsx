// #513 F4 — the popover is the only editor for the stored anomaly threshold,
// and the value it writes is honored by the dashboard and the TUI but NOT by
// `cctally cache-report`. The header comment and the disposition manifest both
// record that divergence, and both are read by developers only. This test pins
// the USER-FACING half: the rendered popover must state the divergence itself.
//
// The assertions read the dialog's rendered text, never a comment or a
// constant, and the fixture threshold is 25 so that the `15` assertion can only
// be satisfied by the disclosure naming the command's own default (an <input>
// value contributes nothing to textContent).
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CacheReportSettings } from './CacheReportSettings';

function renderPopover() {
  render(<CacheReportSettings current_threshold_pp={25} onClose={() => {}} />);
  return screen.getByRole('dialog', { name: 'Cache Report settings' });
}

describe('CacheReportSettings CLI-divergence disclosure (#513 F4)', () => {
  it('names the surfaces that honor the stored threshold', () => {
    const text = renderPopover().textContent ?? '';
    expect(text).toMatch(/dashboard/i);
    expect(text).toMatch(/TUI/);
  });

  it('states that cctally cache-report does not read this value', () => {
    const text = renderPopover().textContent ?? '';
    expect(text).toContain('cctally cache-report');
    expect(text).toMatch(/does not read/i);
  });

  it('names the flag and default the command uses instead', () => {
    const text = renderPopover().textContent ?? '';
    expect(text).toContain('--anomaly-threshold-pp');
    expect(text).toContain('15');
  });

  it('keeps the disclosure free of internal vocabulary', () => {
    const text = renderPopover().textContent ?? '';
    for (const term of ['leaf', 'allowlist', 'endpoint', 'dotted path', 'Class 3']) {
      expect(text.toLowerCase()).not.toContain(term.toLowerCase());
    }
  });
});
