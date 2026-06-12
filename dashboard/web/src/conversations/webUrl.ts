// #177 S4: shared URL helpers for the web-tool cards (WebFetchCard /
// WebSearchCard). Their bespoke anchors bypass the <Markdown> component's
// link pipeline, so the http(s)-only gate lives here (Codex F6) — anything
// else (javascript:, ftp:, malformed) renders as plain text at the call
// sites. domainOf powers the quiet domain text; domains are TEXT only, never
// favicon fetches (the reader makes no external requests on its own).

export function domainOf(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

export function isHttpUrl(url: string): boolean {
  return /^https?:\/\//i.test(url);
}
