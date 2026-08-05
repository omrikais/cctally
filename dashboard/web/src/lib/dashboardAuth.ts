export class DashboardAuthError extends Error {
  constructor() {
    super('Dashboard authentication failed');
    this.name = 'DashboardAuthError';
  }
}

function clearTokenFragment(): void {
  history.replaceState(history.state, '', `${location.pathname}${location.search}`);
}

/** Exchange a one-use URL fragment for the server's HttpOnly API cookie. */
export async function bootstrapDashboardAuth(): Promise<void> {
  const hash = location.hash;
  if (!hash.startsWith('#token=')) return;

  // Remove the credential before decoding or starting network I/O so it never
  // survives in copied URLs, browser history, or an error screen.
  clearTokenFragment();
  const encoded = hash.slice('#token='.length);
  if (!encoded || encoded.includes('&')) throw new DashboardAuthError();

  let token: string;
  try {
    token = decodeURIComponent(encoded);
  } catch {
    throw new DashboardAuthError();
  }
  if (!token) throw new DashboardAuthError();

  let response: Response;
  try {
    response = await fetch('/api/auth', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new DashboardAuthError();
  }
  if (response.status !== 204) throw new DashboardAuthError();
}

/** Paint a deliberately credential-free terminal bootstrap failure. */
export function renderDashboardAuthFailure(root: HTMLElement): void {
  const section = document.createElement('section');
  section.className = 'app-error';
  section.setAttribute('role', 'alert');

  const heading = document.createElement('h1');
  heading.textContent = 'Dashboard authentication failed';
  const detail = document.createElement('p');
  detail.textContent = 'Reopen the dashboard using a fresh URL from its terminal.';

  section.append(heading, detail);
  root.replaceChildren(section);
}
