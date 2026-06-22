import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { BookmarkButton } from './BookmarkButton';
import { _resetForTests, dispatch, getState } from '../store/store';
import { clearBookmarks } from '../store/bookmarks';

beforeEach(() => { _resetForTests(); clearBookmarks(); dispatch({ type: 'OPEN_CONVERSATION', sessionId: 's1' }); });
afterEach(() => { _resetForTests(); clearBookmarks(); });

describe('BookmarkButton', () => {
  it('toggles aria-pressed and dispatches TOGGLE_BOOKMARK', () => {
    const { container } = render(<BookmarkButton sessionId="s1" uuid="u1" />);
    const btn = container.querySelector('.conv-bookmark-btn') as HTMLButtonElement;
    expect(btn.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(btn);
    expect('u1' in getState().convBookmarks).toBe(true);
    expect(container.querySelector('.conv-bookmark-btn')!.getAttribute('aria-pressed')).toBe('true');
  });
  it('saves a note on Enter and sets inputMode while editing', () => {
    dispatch({ type: 'TOGGLE_BOOKMARK', uuid: 'u1' });
    const { container } = render(<BookmarkButton sessionId="s1" uuid="u1" />);
    fireEvent.click(container.querySelector('.conv-bookmark-note-toggle')!);
    const input = container.querySelector('.conv-bookmark-note-input') as HTMLInputElement;
    expect(getState().inputMode).toBe('note'); // suppression contract
    fireEvent.change(input, { target: { value: 'look here' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(getState().convBookmarks.u1.note).toBe('look here');
    expect(getState().inputMode).toBeNull();
  });
  it('cancels a note edit on Escape without saving', () => {
    dispatch({ type: 'TOGGLE_BOOKMARK', uuid: 'u1' });
    const { container } = render(<BookmarkButton sessionId="s1" uuid="u1" />);
    fireEvent.click(container.querySelector('.conv-bookmark-note-toggle')!);
    const input = container.querySelector('.conv-bookmark-note-input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'discard me' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(getState().convBookmarks.u1.note).toBe('');
    expect(getState().inputMode).toBeNull();
  });
  it('does NOT save the discarded draft when Escape is followed by the unmount blur', () => {
    // Regression (#217 S6 F4, real browser): Escape sets editing=false, which
    // unmounts the <input>; the browser then fires a native blur on the way out.
    // A naive onBlur=closeEditor(true) SAVES the draft the user just discarded;
    // the cancelledRef guard must swallow that blur. NOTE: jsdom does NOT route a
    // post-unmount blur to React's onBlur (the synthetic-event root delegation
    // drops it once the fiber detaches), so the trailing fireEvent.blur here is a
    // no-op under jsdom and this test exercises only the Escape-discard half. The
    // guard itself is browser-verified (Playwright). The keyDown assertion still
    // goes RED if Escape ever saves, so the test is non-vacuous.
    dispatch({ type: 'TOGGLE_BOOKMARK', uuid: 'u1' });
    const { container } = render(<BookmarkButton sessionId="s1" uuid="u1" />);
    fireEvent.click(container.querySelector('.conv-bookmark-note-toggle')!);
    const input = container.querySelector('.conv-bookmark-note-input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'discard me' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.blur(input); // the unmount blur the real DOM fires after Escape (no-op under jsdom)
    expect(getState().convBookmarks.u1.note).toBe('');
    expect(getState().inputMode).toBeNull();
  });
  it('saves the draft on a plain blur (no Escape)', () => {
    dispatch({ type: 'TOGGLE_BOOKMARK', uuid: 'u1' });
    const { container } = render(<BookmarkButton sessionId="s1" uuid="u1" />);
    fireEvent.click(container.querySelector('.conv-bookmark-note-toggle')!);
    const input = container.querySelector('.conv-bookmark-note-input') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'keep me' } });
    fireEvent.blur(input);
    expect(getState().convBookmarks.u1.note).toBe('keep me');
    expect(getState().inputMode).toBeNull();
  });
});
