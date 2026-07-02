import { describe, it, expect } from 'vitest';
import { PANEL_REGISTRY, DEFAULT_PANEL_ORDER } from '../src/lib/panelRegistry';

describe('panelRegistry', () => {
  it('DEFAULT_PANEL_ORDER has all 8 grid ids in canonical bento order', () => {
    // #264 S1 — the #248 two-tier order is replaced by the height-matched
    // bento's left-to-right reading order: tall (sessions·trend·projects) →
    // medium (history·cache-report) → short (forecast·blocks·alerts).
    // Existing users keep their persisted order (reconcile preserves
    // positions); only fresh/reset installs get this default.
    expect(DEFAULT_PANEL_ORDER).toEqual([
      'sessions', 'trend', 'projects',
      'history', 'cache-report',
      'forecast', 'blocks', 'alerts',
    ]);
  });

  it('PANEL_REGISTRY has an entry for every PanelId in DEFAULT_PANEL_ORDER', () => {
    for (const id of DEFAULT_PANEL_ORDER) {
      const def = PANEL_REGISTRY[id];
      expect(def).toBeTruthy();
      expect(def.id).toBe(id);
      expect(typeof def.label).toBe('string');
      expect(def.label.length).toBeGreaterThan(0);
      expect(typeof def.Component).toBe('function');
      expect(typeof def.openAction).toBe('function');
    }
  });

  it('every PanelId has a unique label', () => {
    const labels = DEFAULT_PANEL_ORDER.map((id) => PANEL_REGISTRY[id].label);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it('DEFAULT_PANEL_ORDER matches PANEL_REGISTRY keys exactly', () => {
    const registryKeys = Object.keys(PANEL_REGISTRY);
    expect(DEFAULT_PANEL_ORDER.length).toBe(registryKeys.length);
    expect(new Set(DEFAULT_PANEL_ORDER)).toEqual(new Set(registryKeys));
  });

  it('DEFAULT_PANEL_ORDER has no duplicate ids', () => {
    expect(new Set(DEFAULT_PANEL_ORDER).size).toBe(DEFAULT_PANEL_ORDER.length);
  });
});
