/** The IANA zone that nearby instants were converted into.
 *
 * Numeric offsets and abbreviations cannot identify a zone: `+03` is shared
 * by several zones, and a zone that is `+03` in August may be `+02` in
 * January. Callers pass the server-resolved display zone that actually drove
 * their formatter context.
 */
export function ZoneTag({ tz }: { tz: string }): JSX.Element {
  return <span className="instant-zone">[{tz}]</span>;
}
