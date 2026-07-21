import { useState } from 'react';
import { ImageIcon, DocumentIcon } from './ConvIcons';
import { useConversationRef } from './TranscriptContext';
import { conversationEntityUrl } from '../lib/conversationTransport';
import type { MediaRef } from '../types/conversation';

// #177 S4 (Q7-A): inline lazy-loaded media. Addressing: exactly one of
// toolUseId (result-side media) / uuid (user-content media) + the
// ingest-stamped ordinal `index`. Renders the figure only when addressable;
// otherwise — and on any fetch error (404/410/413, caught by <img onError>
// with no extra round trip) — degrades to the pre-S4 byte-count badge.
// Documents never render an <img>: badge + open-in-new-tab (Q4).

// bytes is the BASE64 length; decoded ≈ ×3/4 (spec §4.4 caption math).
function approxSize(b64len: number): string {
  const bytes = Math.floor((b64len * 3) / 4);
  if (bytes >= 1024 * 1024) return `~${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `~${Math.round(bytes / 1024)} KB`;
  return `~${bytes} B`;
}

export function MediaFigure({
  media,
  toolUseId,
  uuid,
  context,
}: {
  media: MediaRef;
  toolUseId?: string | null;
  uuid?: string | null;
  context: string;
}) {
  const conversationRef = useConversationRef();
  const [failed, setFailed] = useState(false);
  // #217 S6 F9 — inline PDF expand state. Collapsed by default so the <object>
  // (and thus the byte fetch) mounts only on demand; a transcript with several
  // PDFs stays light until the user expands one.
  const [pdfOpen, setPdfOpen] = useState(false);

  const key = toolUseId
    ? `tool_use_id=${encodeURIComponent(toolUseId)}`
    : uuid
      ? `uuid=${encodeURIComponent(uuid)}`
      : null;
  const addressable =
    conversationRef != null && key != null && Number.isInteger(media.index) && media.index >= 0;

  if (!addressable || failed) {
    return (
      <span className="conv-chip conv-chip--media">
        {media.kind === 'image' ? <ImageIcon /> : <DocumentIcon />}{' '}
        {media.media_type ?? media.kind} · {media.bytes} B
        {failed && <span className="conv-media-gone"> · source no longer available</span>}
      </span>
    );
  }
  const url = conversationEntityUrl(conversationRef!, 'media', {
    ...(toolUseId ? { tool_use_id: toolUseId } : { uuid: uuid! }),
    index: media.index,
  });

  if (media.kind === 'document') {
    // #217 S6 F9 — only application/pdf gets the inline click-to-expand option;
    // every other document type (and the `failed` path above) keeps today's
    // badge verbatim. The media route already serves application/pdf with a
    // Content-Type + inline disposition and no CSP sandbox, so the <object>
    // reuses the same gated URL — no backend/privacy change. <object>'s JS
    // onError is unreliable, so the declarative fallback CHILD (the open-↗ link)
    // renders automatically when the browser has no PDF viewer.
    const isPdf = media.media_type === 'application/pdf';
    // #217 S6 F9 (review) — the <object> id (and its aria-controls) must be unique
    // within the document. `index` alone collides when one turn carries two PDFs
    // addressed by different keys (e.g. a tool_use_id and a uuid) that happen to
    // share the same ordinal, so fold the addressing key into the id.
    const pdfDomId = `conv-pdf-${toolUseId ?? uuid ?? 'x'}-${media.index}`;
    return (
      <span className="conv-chip conv-chip--media conv-doc">
        <DocumentIcon /> {media.media_type ?? 'document'} · {approxSize(media.bytes)} ·{' '}
        {isPdf && (
          <>
            <button
              type="button"
              className="conv-pdf-toggle"
              aria-expanded={pdfOpen}
              aria-controls={pdfDomId}
              onClick={() => setPdfOpen((v) => !v)}
            >
              {pdfOpen ? 'collapse ▴' : 'view inline ▾'}
            </button>{' · '}
          </>
        )}
        <a href={url} target="_blank" rel="noopener noreferrer">open ↗</a>
        {isPdf && pdfOpen && (
          <object id={pdfDomId} className="conv-pdf-inline" data={url} type="application/pdf" aria-label={`PDF preview ${media.index + 1}`}>
            <a href={url} target="_blank" rel="noopener noreferrer">open ↗</a>
          </object>
        )}
      </span>
    );
  }
  return (
    <figure className="conv-media-figure">
      <img
        src={url}
        loading="lazy"
        decoding="async"
        alt={`${context} image ${media.index + 1} (${media.media_type ?? 'image'})`}
        onError={() => setFailed(true)}
      />
      <figcaption className="conv-media-caption">
        <span>{media.media_type ?? 'image'}</span>
        <span>{approxSize(media.bytes)}</span>
        <a href={url} target="_blank" rel="noopener noreferrer">open full size ↗</a>
      </figcaption>
    </figure>
  );
}
