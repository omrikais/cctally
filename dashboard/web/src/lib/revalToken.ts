import type { Envelope } from '../types/envelope';

// #300 — the revalidation token the dashboard's lazy detail fetchers key their
// SWR refetch effect on, instead of the 5s `generated_at` heartbeat.
//
// Prefer the all-inputs `data_version` (a compact string derived from the DB
// dispatch signature), which changes iff any DB leg the detail endpoints read
// actually changed (session entries, weekly usage/cost, reset events, codex
// entries, cache generation) — so a finished/static session/project/
// conversation open in a modal/reader fetches once and is not re-GET on every
// tick. It is GLOBAL, so it over-invalidates on unrelated ingests (any
// session's growth changes it); that is the accepted trade-off and still
// strictly fewer refetches than every-tick.
//
// An empty (or absent) `data_version` is the "no real signal" sentinel — a
// Python without the field, or the non-precompute path — and MUST fall back to
// `generated_at` (today's every-tick behavior), which costs nothing real
// because that state has no live detail to thrash.
export function revalToken(
  env: Pick<Envelope, 'data_version' | 'generated_at'> | null | undefined,
): string {
  const dv = env?.data_version;
  if (typeof dv === 'string' && dv !== '') return dv;
  return env?.generated_at ?? '';
}
