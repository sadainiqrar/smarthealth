import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorisedHandler } from "./api";

function respond(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), { status })));
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("api", () => {
  it("prefixes /api and sends the bearer token", async () => {
    sessionStorage.setItem("smarthealth.token", "t0k");
    respond(200, { ok: true });
    await api("/providers");
    const [url, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/providers");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t0k");
  });

  it("turns the error envelope into an ApiError", async () => {
    respond(409, { error: "Conflict", detail: "slot is no longer free" });
    await expect(api("/x")).rejects.toMatchObject({
      status: 409, error: "Conflict", detail: "slot is no longer free",
    });
  });

  it("stringifies a validation detail list", async () => {
    respond(422, { detail: [{ loc: ["body", "slot_id"], msg: "missing" }] });
    const err = (await api("/x").catch((e) => e)) as ApiError;
    expect(err.status).toBe(422);
    expect(err.detail).toContain("slot_id");
  });

  it("calls the unauthorised handler on 401", async () => {
    const handler = vi.fn();
    setUnauthorisedHandler(handler);
    respond(401, { error: "InvalidCredentials", detail: "missing or invalid credentials" });
    await expect(api("/x")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
  });
});

describe("refreshing an expired session", () => {
  function sequence(...responses: Array<[number, unknown]>) {
    const queue = [...responses];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        const [status, body] = queue.shift() ?? [500, {}];
        return new Response(JSON.stringify(body), { status });
      }),
    );
    return fetch as unknown as ReturnType<typeof vi.fn>;
  }

  it("refreshes once on a 401 and replays the request with the new token", async () => {
    sessionStorage.setItem("smarthealth.token", "old");
    sessionStorage.setItem("smarthealth.refresh", "r1");
    const calls = sequence(
      [401, { error: "InvalidCredentials", detail: "expired" }],
      [200, { access_token: "new", refresh_token: "r2" }],
      [200, { ok: true }],
    );
    await expect(api("/providers")).resolves.toEqual({ ok: true });
    expect(calls.mock.calls[1][0]).toBe("/api/auth/refresh");
    expect(JSON.parse(calls.mock.calls[1][1].body)).toEqual({ refresh_token: "r1" });
    expect(new Headers(calls.mock.calls[2][1].headers).get("Authorization")).toBe("Bearer new");
    expect(sessionStorage.getItem("smarthealth.refresh")).toBe("r2");
  });

  it("logs out when the refresh itself is refused", async () => {
    const handler = vi.fn();
    setUnauthorisedHandler(handler);
    sessionStorage.setItem("smarthealth.token", "old");
    sessionStorage.setItem("smarthealth.refresh", "spent");
    sequence(
      [401, { error: "InvalidCredentials", detail: "expired" }],
      [401, { error: "InvalidCredentials", detail: "refresh token is not valid" }],
    );
    await expect(api("/providers")).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledOnce();
  });

  it("never refreshes for a failed login", async () => {
    sessionStorage.setItem("smarthealth.refresh", "r1");
    const calls = sequence([401, { error: "InvalidCredentials", detail: "wrong" }]);
    await expect(api("/auth/login", { method: "POST", json: {} })).rejects.toBeInstanceOf(ApiError);
    expect(calls).toHaveBeenCalledOnce();
  });

  it("spends the refresh token once when two requests expire together", async () => {
    sessionStorage.setItem("smarthealth.token", "old");
    sessionStorage.setItem("smarthealth.refresh", "r1");
    const calls = sequence(
      [401, { error: "InvalidCredentials", detail: "expired" }],
      [401, { error: "InvalidCredentials", detail: "expired" }],
      [200, { access_token: "new", refresh_token: "r2" }],
      [200, { a: 1 }],
      [200, { b: 2 }],
    );
    await Promise.all([api("/a"), api("/b")]);
    const refreshes = calls.mock.calls.filter(([url]) => url === "/api/auth/refresh");
    expect(refreshes).toHaveLength(1);
  });
});
