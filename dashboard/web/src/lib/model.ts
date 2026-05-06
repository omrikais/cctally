export type ModelChipClass = 'opus' | 'haiku' | 'sonnet';

export function modelChipClass(m: string | null | undefined): ModelChipClass {
  if (!m) return 'sonnet';
  if (m.includes('opus')) return 'opus';
  if (m.includes('haiku')) return 'haiku';
  return 'sonnet';
}
