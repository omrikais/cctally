import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render } from '@testing-library/react';
import { HELP_ROWS } from './HelpOverlay';
import { SessionsControls } from './SessionsControls';
import { buildGlobalKeyBindings } from '../store/globalBindings';
import { _resetForTests } from '../store/store';
import {
  _resetForTests as _resetKeymap,
  installGlobalKeydown,
  uninstallGlobalKeydown,
  registerKeymap,
  registeredBindings,
} from '../store/keymap';

const documented = new Set(HELP_ROWS.flatMap((r) => r.keys));

describe('Help table documents every user-facing global/sessions hotkey (D1)', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    _resetKeymap();
    installGlobalKeydown();
  });
  afterEach(() => { uninstallGlobalKeydown(); });

  it('covers all single-key global/sessions bindings (digits excluded)', () => {
    // Register production bindings without booting main.tsx:
    registerKeymap(buildGlobalKeyBindings());
    render(<SessionsControls />);   // registers f and / via useKeymap
    const missing = registeredBindings()
      .filter((b) => (b.scope === 'global' || b.scope === 'sessions'))
      .filter((b) => b.view !== 'conversations')
      .filter((b) => b.key.length === 1 && !/[0-9]/.test(b.key)) // digits are the positional row
      .map((b) => b.key)
      .filter((k) => !documented.has(k));
    expect(missing).toEqual([]);
  });
});
