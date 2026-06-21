import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { FilesTab } from './FilesTab';
import type { OutlineFile } from '../types/conversation';

// #217 S5 F2 — the Files tab inside the outline panel. Lists each modified file
// (basename prominent, dir muted), a +N -M badge (a null side omitted), and an
// expandable touches list whose rows jump to the touch's turn anchor.
const files: OutlineFile[] = [
  {
    path: 'bin/foo.py',
    add: 42,
    del: 18,
    touches: [
      { uuid: 'u1', tool_use_id: 't1', op: 'edit', add: 40, del: 18 },
      { uuid: 'u2', tool_use_id: 't2', op: 'edit', add: 2, del: 0 },
    ],
  },
];

describe('FilesTab', () => {
  it('renders the basename, the dir, and the +N -M badge', () => {
    render(<FilesTab files={files} onJump={() => {}} />);
    expect(screen.getByText('foo.py')).toBeInTheDocument();
    expect(screen.getByText('bin/')).toBeInTheDocument();
    expect(screen.getByText(/\+42/)).toBeInTheDocument();
    expect(screen.getByText(/[−-]18/)).toBeInTheDocument();
  });

  it('expands to its touches and jumps on a touch-row click', () => {
    const onJump = vi.fn();
    render(<FilesTab files={files} onJump={onJump} />);
    // Expand the file to reveal its touches.
    fireEvent.click(screen.getByRole('button', { name: /foo\.py/i }));
    const touchRows = screen.getAllByRole('button', { name: /edit/i });
    expect(touchRows.length).toBe(2);
    fireEvent.click(touchRows[0]);
    expect(onJump).toHaveBeenCalledWith('u1');
  });

  it('omits a null side of the badge but still lists the touch', () => {
    const f: OutlineFile[] = [
      { path: 'x.py', add: null, del: null, touches: [{ uuid: 'u', tool_use_id: null, op: 'edit', add: null, del: null }] },
    ];
    render(<FilesTab files={f} onJump={() => {}} />);
    // No numeric badge values (both sides null), but the file row exists.
    expect(screen.getByText('x.py')).toBeInTheDocument();
    expect(screen.queryByText(/\+\d/)).toBeNull();
  });

  it('renders a Write op with del 0', () => {
    const f: OutlineFile[] = [
      { path: 'new.py', add: 5, del: 0, touches: [{ uuid: 'w', tool_use_id: 'tw', op: 'write', add: 5, del: 0 }] },
    ];
    render(<FilesTab files={f} onJump={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: /new\.py/i }));
    expect(screen.getByRole('button', { name: /write/i })).toBeInTheDocument();
  });

  it('shows the empty state when no files were modified', () => {
    render(<FilesTab files={[]} onJump={() => {}} />);
    expect(screen.getByText(/No files modified/i)).toBeInTheDocument();
  });
});
