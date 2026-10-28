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
