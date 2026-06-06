// alertAxis chip/title labels — fifth axis (project_budget, issue #19/#121).
//
// The chip/title maps are the single client-side source of truth for axis
// labels (Toast / RecentAlertsPanel / RecentAlertsModal read them by axis id).
// They MUST stay byte-identical with the Python kernel
// bin/_lib_alert_axes.py AXIS_REGISTRY — a Python↔TS parity test
// (tests/test_alert_axes_chip_parity.py) asserts that cross-language. This
// Vitest pins the TS side so a stray edit to the map is caught in the JS suite
// too, and specifically that `project_budget` resolves to "PROJECT".
import { describe, expect, it } from 'vitest';
import { AXIS_CHIP_LABEL, AXIS_TITLE_LABEL } from './alertAxis';
import type { AlertAxis } from '../types/envelope';

describe('AXIS_CHIP_LABEL / AXIS_TITLE_LABEL', () => {
  it('resolves the project_budget axis to the PROJECT chip', () => {
    const axis: AlertAxis = 'project_budget';
    expect(AXIS_CHIP_LABEL[axis]).toBe('PROJECT');
    expect(AXIS_TITLE_LABEL[axis]).toBe('Project budget');
  });

  it('resolves the codex_budget axis to the CODEX chip', () => {
    const axis: AlertAxis = 'codex_budget';
    expect(AXIS_CHIP_LABEL[axis]).toBe('CODEX');
    expect(AXIS_TITLE_LABEL[axis]).toBe('Codex budget');
  });

  it('covers all six axes with the expected chip + title labels', () => {
    // Byte-identical with bin/_lib_alert_axes.py AXIS_REGISTRY
    // (chip_label / title_label). The Python-side parity test asserts the
    // cross-language match; this freezes the TS values.
    expect(AXIS_CHIP_LABEL).toEqual({
      weekly: 'WEEKLY',
      five_hour: '5H-BLOCK',
      budget: 'BUDGET',
      projected: 'PROJECTED',
      project_budget: 'PROJECT',
      codex_budget: 'CODEX',
    });
    expect(AXIS_TITLE_LABEL).toEqual({
      weekly: 'Weekly',
      five_hour: '5h-block',
      budget: 'Budget',
      projected: 'Projected',
      project_budget: 'Project budget',
      codex_budget: 'Codex budget',
    });
  });
});
