import { describe, expect, it } from "vitest";
import { decodeToken } from "./auth";

function token(payload: object): string {
  const b64 = (o: object) =>
    btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64(payload)}.sig`;
}

describe("decodeToken", () => {
  it("reads role, subject and expiry", () => {
    expect(decodeToken(token({ sub: "u1", role: "front_desk", exp: 2000000000 }))).toEqual({
      subject: "u1", role: "front_desk", expiresAt: 2000000000 * 1000,
    });
  });

  it("returns null for garbage", () => {
    expect(decodeToken("not.a.jwt")).toBeNull();
  });
});
