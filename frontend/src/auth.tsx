// Session state. The token is kept in sessionStorage (cleared with the tab). Its payload
// is decoded for DISPLAY only -- which nav items to show. The API re-verifies every
// request, so a forged payload buys nothing (spec F-2).
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  api,
  readToken,
  setTokensChangedHandler,
  setUnauthorisedHandler,
  storeTokens,
} from "./api";

export type Role = "patient" | "provider" | "front_desk" | "admin";
const ROLES: readonly string[] = ["patient", "provider", "front_desk", "admin"];
export interface Session {
  subject: string;
  role: Role;
  expiresAt: number;
}

export function decodeToken(token: string): Session | null {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(part.padEnd(part.length + ((4 - (part.length % 4)) % 4), "=")));
    if (typeof payload.sub !== "string" || !ROLES.includes(payload.role)) return null;
    return { subject: payload.sub, role: payload.role as Role, expiresAt: payload.exp * 1000 };
  } catch {
    return null;
  }
}

interface AuthValue {
  session: Session | null;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(() => {
    const token = readToken();
    // An expired access token still identifies the session: the first request refreshes
    // it (api.ts), and only a failed refresh logs the user out.
    return token ? decodeToken(token) : null;
  });

  const queryClient = useQueryClient();

  // The cache holds the previous user's patients and appointments; drop it on every
  // identity change so the next login never sees it.
  const clearLocal = useCallback(() => {
    storeTokens(null, null);
    setSession(null);
    queryClient.clear();
  }, [queryClient]);

  // Ends the session on the server too, so the tokens stop working everywhere, not just
  // in this tab. Fire and forget: the local state is cleared whatever the answer.
  const logout = useCallback(() => {
    if (readToken()) void api("/auth/logout", { method: "POST" }, true).catch(() => {});
    clearLocal();
  }, [clearLocal]);

  useEffect(() => setUnauthorisedHandler(clearLocal), [clearLocal]);
  useEffect(() => setTokensChangedHandler((access) => setSession(decodeToken(access))), []);

  const login = useCallback(async (email: string, password: string) => {
    const { access_token, refresh_token } = await api<{
      access_token: string;
      refresh_token: string;
    }>("/auth/login", {
      method: "POST",
      json: { email, password },
    });
    queryClient.clear();
    storeTokens(access_token, refresh_token);
    setSession(decodeToken(access_token));
  }, [queryClient]);

  const value = useMemo(() => ({ session, login, logout }), [session, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}
