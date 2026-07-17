import { describe, expect, it } from 'vitest';
import { rovingAction } from './sessionsRovingKeyboard';

// #299 — pure roving decision for the Sessions grid (row-primary, grid-lite).
const onRow = (cellCount: number) => ({ onRow: true, cellIdx: -1, cellCount });
const onCell = (cellIdx: number, cellCount: number) => ({ onRow: false, cellIdx, cellCount });

describe('rovingAction — row axis', () => {
  it('Up/Down/Home/End return relative row intents regardless of focus mode', () => {
    expect(rovingAction('ArrowDown', onRow(4))).toEqual({ kind: 'row', to: 'next' });
    expect(rovingAction('ArrowUp', onRow(4))).toEqual({ kind: 'row', to: 'prev' });
    expect(rovingAction('Home', onRow(4))).toEqual({ kind: 'row', to: 'first' });
    expect(rovingAction('End', onRow(4))).toEqual({ kind: 'row', to: 'last' });
    // Also collapses to row mode from a cell:
    expect(rovingAction('ArrowDown', onCell(2, 4))).toEqual({ kind: 'row', to: 'next' });
  });
});

describe('rovingAction — cell axis', () => {
  it('Right from the row enters the first control (or null when none)', () => {
    expect(rovingAction('ArrowRight', onRow(4))).toEqual({ kind: 'cell', to: 0 });
    expect(rovingAction('ArrowRight', onRow(0))).toBeNull();
  });
  it('Right advances but clamps at the last control (no wrap)', () => {
    expect(rovingAction('ArrowRight', onCell(1, 4))).toEqual({ kind: 'cell', to: 2 });
    expect(rovingAction('ArrowRight', onCell(3, 4))).toBeNull();
  });
  it('Left retreats, and from cell 0 returns focus to the row', () => {
    expect(rovingAction('ArrowLeft', onCell(2, 4))).toEqual({ kind: 'cell', to: 1 });
    expect(rovingAction('ArrowLeft', onCell(0, 4))).toEqual({ kind: 'rowFocus' });
    expect(rovingAction('ArrowLeft', onRow(4))).toBeNull();
  });
});

describe('rovingAction — activation & pass-through', () => {
  it('Enter/Space on the row activates it; on a cell returns null (native fires)', () => {
    expect(rovingAction('Enter', onRow(4))).toEqual({ kind: 'activateRow' });
    expect(rovingAction(' ', onRow(4))).toEqual({ kind: 'activateRow' });
    expect(rovingAction('Enter', onCell(0, 4))).toBeNull();
    expect(rovingAction(' ', onCell(0, 4))).toBeNull();
  });
  it('returns null for unhandled keys', () => {
    expect(rovingAction('a', onRow(4))).toBeNull();
    expect(rovingAction('Tab', onRow(4))).toBeNull();
    expect(rovingAction('PageDown', onCell(1, 4))).toBeNull();
  });
});
