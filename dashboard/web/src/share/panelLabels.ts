// Display labels + filename extensions for the share modal. Mirrors the
// `panelLabel` props sprinkled across the 8 share-capable panel/modal
// components so the share modal can title itself without depending on
// the caller (the share modal only knows the panel id from
// `state.shareModal.panel`).
//
// Keep in sync with panels/<Panel>.tsx — these strings are user-facing.
import type { ShareFormat, SharePanelId } from './types';

export const SHARE_PANEL_LABELS: Record<SharePanelId, string> = {
  'current-week': 'Current week',
  trend: 'Trend',
  weekly: 'Weekly',
  daily: 'Daily',
  monthly: 'Monthly',
  blocks: '5-hour blocks',
  forecast: 'Forecast',
  sessions: 'Sessions',
};

export function sharePanelLabel(panel: SharePanelId): string {
  return SHARE_PANEL_LABELS[panel];
}

// File-extension mapping for the Download anchor (spec §6.5 "filename
// cctally-<panel>-<utcdate>.<ext>"). MD → .md, HTML → .html, SVG → .svg.
// Matches what bin/_lib_share.py emits.
export function shareFormatExt(format: ShareFormat): string {
  switch (format) {
    case 'md':
      return 'md';
    case 'html':
      return 'html';
    case 'svg':
      return 'svg';
  }
}
