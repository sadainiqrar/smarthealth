import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it } from "vitest";
import { AuthProvider, useAuth } from "./auth";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("logout empties the query cache so the next login cannot see it", () => {
  const client = new QueryClient();
  client.setQueryData(["patients", "", 0], { items: [{ id: "p1" }] });
  let logout: () => void = () => {};
  function Probe() {
    logout = useAuth().logout;
    return null;
  }
  const root = createRoot(document.createElement("div"));
  act(() => {
    root.render(
      <QueryClientProvider client={client}>
        <AuthProvider><Probe /></AuthProvider>
      </QueryClientProvider>,
    );
  });
  expect(client.getQueryCache().getAll()).toHaveLength(1);
  act(() => logout());
  expect(client.getQueryCache().getAll()).toHaveLength(0);
  act(() => root.unmount());
});
