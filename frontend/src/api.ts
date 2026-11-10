// One fetch wrapper for the whole client. Every failure becomes an ApiError carrying the
// API's own {error, detail} -- the UI shows server rules verbatim, never its own wording.

export const TOKEN_KEY = "smarthealth.token";
// The refresh token (Week 3): single use, spent by POST /auth/refresh for a new pair. Kept
// in sessionStorage beside the access token -- an HttpOnly cookie is the production
// choice and is deferred (it needs CSRF handling across the dev proxy).
export const REFRESH_KEY = "smarthealth.refresh";

export class ApiError extends Error {
  constructor(public status: number, public error: string, public detail: string) {
    super(`${error}: ${detail}`);
  }
}

let onUnauthorised: () => void = () => {};
export function setUnauthorisedHandler(handler: () => void): void {
  onUnauthorised = handler;
}

let onTokensChanged: (access: string) => void = () => {};
export function setTokensChangedHandler(handler: (access: string) => void): void {
  onTokensChanged = handler;
}

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

export function readToken(): string | null {
  return read(TOKEN_KEY);
}

export function storeTokens(access: string | null, refresh: string | null): void {
  try {
    if (access) sessionStorage.setItem(TOKEN_KEY, access);
    else sessionStorage.removeItem(TOKEN_KEY);
    if (refresh) sessionStorage.setItem(REFRESH_KEY, refresh);
    else sessionStorage.removeItem(REFRESH_KEY);
  } catch {
    // storage unavailable (private mode): the session lasts until reload
  }
}

// One refresh in flight at a time. A refresh token is single use: two concurrent requests
// both refreshing would spend it twice, and the server treats the second as a replay and
// ends the session.
let refreshing: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  const refresh = read(REFRESH_KEY);
  if (!refresh) return false;
  const response = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!response.ok) return false;
  const tokens = (await response.json()) as { access_token: string; refresh_token: string };
  storeTokens(tokens.access_token, tokens.refresh_token);
  onTokensChanged(tokens.access_token);
  return true;
}

function refreshOnce(): Promise<boolean> {
  refreshing ??= refreshTokens()
    .catch(() => false)
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

type Options = Omit<RequestInit, "body"> & { json?: unknown };

export async function api<T>(path: string, options: Options = {}, retried = false): Promise<T> {
  const { json, ...init } = options;
  const headers = new Headers(init.headers);
  const token = readToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body: BodyInit | undefined;
  if (json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(json);
  }
  const response = await fetch(`/api${path}`, { ...init, headers, body });
  if (!response.ok) {
    let error = "HTTPError";
    let detail = response.statusText;
    try {
      const payload = await response.json();
      error = payload.error ?? error;
      detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      // non-JSON body (e.g. a proxy error page): keep the status text
    }
    if (response.status === 401) {
      // An expired access token (they live 10 minutes): refresh once and replay the
      // request. Never for /auth/* itself -- a failed login is not an expired session.
      if (!retried && !path.startsWith("/auth/") && (await refreshOnce())) {
        return api<T>(path, options, true);
      }
      onUnauthorised();
    }
    throw new ApiError(response.status, error, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
