// One fetch wrapper for the whole client. Every failure becomes an ApiError carrying the
// API's own {error, detail} -- the UI shows server rules verbatim, never its own wording.

export const TOKEN_KEY = "smarthealth.token";

export class ApiError extends Error {
  constructor(public status: number, public error: string, public detail: string) {
    super(`${error}: ${detail}`);
  }
}

let onUnauthorised: () => void = () => {};
export function setUnauthorisedHandler(handler: () => void): void {
  onUnauthorised = handler;
}

export function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

type Options = Omit<RequestInit, "body"> & { json?: unknown };

export async function api<T>(path: string, options: Options = {}): Promise<T> {
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
    if (response.status === 401) onUnauthorised();
    throw new ApiError(response.status, error, detail);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}
