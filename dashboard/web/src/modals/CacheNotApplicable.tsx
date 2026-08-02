// A cache figure that does not exist for this provider, with the provider's own
// reason attached (#443 S2 §3.3, §4.4).
//
// Not applicable is not unevaluated. "Unevaluated" means the figure could be
// computed given more data — a thin baseline resolves with time. "Not
// applicable" means the concept does not exist for that provider and never
// will: OpenAI charges no cache-write premium, so a Codex day has nothing
// wasted and therefore no ratio of saved to wasted. Rendering the structural
// zero those fields still carry would be presenting a constant as a
// measurement, which is the error this session exists to remove.
//
// The reason comes from the WIRE's `not_applicable` map, never from a literal
// here, so the copy cannot drift from the provider that owns it. It rides on
// `aria-label` as well as `title` — matching DailyFlagGlyph's precedent — so
// the state reads without depending on colour or glyph alone.
export function CacheNotApplicable({ reason }: { reason: string }) {
  return (
    <span className="m-unavailable" aria-label={reason} title={reason}>n/a</span>
  );
}
