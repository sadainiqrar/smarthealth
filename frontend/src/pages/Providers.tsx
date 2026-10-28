import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { ErrorText, Pager, listPath, useDebounced } from "../shared";
import type { Page, Provider } from "../types";

export default function Providers() {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const term = useDebounced(search);
  const query = useQuery({
    queryKey: ["providers", term, offset],
    queryFn: () => api<Page<Provider>>(listPath("/providers", term, offset)),
  });

  return (
    <section>
      <h1>Providers</h1>
      <input
        type="search" placeholder="Search providers" value={search}
        onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
      />
      <ErrorText error={query.error} />
      {query.data && (
        <>
          <table>
            <thead><tr><th>Name</th><th>Specialty</th><th>Active</th></tr></thead>
            <tbody>
              {query.data.items.map((p) => (
                <tr key={p.id}>
                  <td>{p.first_name} {p.last_name}</td><td>{p.specialty}</td><td>{p.is_active ? "yes" : "no"}</td>
                </tr>
              ))}
              {query.data.items.length === 0 && <tr><td colSpan={3}>No matches.</td></tr>}
            </tbody>
          </table>
          <Pager page={query.data} onOffset={setOffset} />
        </>
      )}
    </section>
  );
}
