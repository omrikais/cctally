import { useState } from 'react';
import type { OutlineFile, OutlineFileTouch } from '../types/conversation';

// #217 S5 F2 — the Files tab inside the outline panel. Lists every file touched
// by an Edit/MultiEdit/Write call (server-aggregated whole-session, document
// order), each with a +N -M badge (a null side omitted) and an expandable list
// of touches. A touch row is a jump button (`op · turn →`) calling `onJump` with
// the touch's turn anchor uuid — reusing the outline's existing loadToTarget/pin
// jump. Empty state: "No files modified". Reuses the .conv-outline-* row styles;
// every interactive row is ≥44px on mobile (CSS).

// The op → short verb label shown on each touch row.
const OP_LABEL: Record<OutlineFileTouch['op'], string> = {
  edit: 'Edit',
  multiedit: 'MultiEdit',
  write: 'Write',
};

// Split a path into (dir-with-trailing-slash, basename). A bare basename yields
// an empty dir. Handles both '/' (POSIX) paths the transcripts carry.
function splitPath(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf('/');
  if (i < 0) return { dir: '', base: path };
  return { dir: path.slice(0, i + 1), base: path.slice(i + 1) };
}

// "+N −M" with a null side omitted (the minus uses the U+2212 minus sign to
// match the diff-badge convention elsewhere). Returns null when both are null.
function StatBadge({ add, del }: { add: number | null; del: number | null }) {
  if (add == null && del == null) return null;
  return (
    <span className="conv-files-stat">
      {add != null && <span className="conv-files-stat-add">+{add}</span>}
      {del != null && <span className="conv-files-stat-del">−{del}</span>}
    </span>
  );
}

function FileRow({
  file,
  onJump,
}: {
  file: OutlineFile;
  onJump: (uuid: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const { dir, base } = splitPath(file.path);
  return (
    <li className="conv-files-item">
      <button
        type="button"
        className="conv-files-file"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        title={file.path}
      >
        <span className="conv-files-disclosure" aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        <span className="conv-files-name">
          {dir && <span className="conv-files-dir">{dir}</span>}
          <span className="conv-files-base">{base}</span>
        </span>
        <StatBadge add={file.add} del={file.del} />
        <span className="conv-files-count" aria-hidden="true">
          ×{file.touches.length}
        </span>
      </button>
      {open && (
        <ul className="conv-files-touches">
          {file.touches.map((t, i) => (
            <li key={`${t.tool_use_id ?? t.uuid}-${i}`}>
              <button
                type="button"
                className="conv-files-touch"
                onClick={() => onJump(t.uuid)}
              >
                <span className="conv-files-touch-op">{OP_LABEL[t.op]}</span>
                <span className="conv-files-touch-sep" aria-hidden="true">
                  ·
                </span>
                <StatBadge add={t.add} del={t.del} />
                <span className="conv-files-touch-jump" aria-hidden="true">
                  →
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function FilesTab({
  files,
  onJump,
}: {
  files: OutlineFile[];
  onJump: (uuid: string) => void;
}) {
  if (files.length === 0) {
    return <div className="conv-files-empty">No files modified</div>;
  }
  return (
    <ul className="conv-files-list">
      {files.map((f) => (
        <FileRow key={f.path} file={f} onJump={onJump} />
      ))}
    </ul>
  );
}
