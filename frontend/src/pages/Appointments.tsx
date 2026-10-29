import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { STATUSES, appointmentsPath, canCancel } from "../appointments";
import { useAuth } from "../auth";
import { ErrorText, PAGE_SIZE, Pager } from "../shared";
import type { Appointment, Page, Provider } from "../types";

// Inline, not window.prompt: a reason is required, and the API's refusal shows beside it.
function CancelForm({ appointment, onDone }: { appointment: Appointment; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const mutation = useMutation({
    mutationFn: () => api<Appointment>(`/appointments/${appointment.id}/cancel`, {
      method: "POST", json: { reason },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["appointments"] });
      queryClient.invalidateQueries({ queryKey: ["appointment", appointment.id] });
      onDone();
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };
  return (
    <form className="cancel-form" onSubmit={submit}>
      <input
        placeholder="Reason for cancelling" value={reason} required maxLength={2000}
        aria-label="Reason for cancelling" onChange={(e) => setReason(e.target.value)}
      />
      <button type="submit" disabled={mutation.isPending || reason.trim() === ""}>Confirm cancel</button>
      <button type="button" onClick={onDone}>Keep</button>
      <ErrorText error={mutation.error} />
    </form>
  );
}

export default function Appointments() {
  const { session } = useAuth();
  const role = session?.role;
  const isPatient = role === "patient";
  const [filters, setFilters] = useState({ status: "", providerId: "" });
  const [offset, setOffset] = useState(0);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["appointments", filters, offset],
    queryFn: () => api<Page<Appointment>>(appointmentsPath(filters, PAGE_SIZE, offset)),
  });
  // Staff pick a provider from the first page of the directory; a patient has no filters.
  const providers = useQuery({
    queryKey: ["providers", "picker"],
    queryFn: () => api<Page<Provider>>("/providers?limit=100"),
    enabled: !isPatient,
  });
  const providerName = (id: string | null) => {
    const p = providers.data?.items.find((x) => x.id === id);
    return p ? `${p.first_name} ${p.last_name}` : id ? id.slice(0, 8) : "";
  };
  const setFilter = (change: Partial<typeof filters>) => {
    setFilters({ ...filters, ...change });
    setOffset(0);
  };
  const columns = isPatient ? 5 : 6;

  return (
    <section>
      <h1>{isPatient ? "My appointments" : "Appointments"}</h1>
      {!isPatient && (
        <div className="filters">
          <label>Status
            <select value={filters.status} onChange={(e) => setFilter({ status: e.target.value })}>
              <option value="">Any</option>
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
          <label>Provider
            <select value={filters.providerId} onChange={(e) => setFilter({ providerId: e.target.value })}>
              <option value="">Any</option>
              {providers.data?.items.map((p) => (
                <option key={p.id} value={p.id}>{p.first_name} {p.last_name}</option>
              ))}
            </select>
          </label>
        </div>
      )}
      <ErrorText error={query.error} />
      {query.data && (
        <>
          <table>
            <thead>
              <tr>
                <th>When</th>{!isPatient && <th>Provider</th>}<th>Status</th><th>Reason</th>
                <th>Booking</th><th></th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((a) => (
                <tr key={a.id}>
                  <td>{a.starts_at ? new Date(a.starts_at).toLocaleString() : "no slot yet"}</td>
                  {!isPatient && <td>{providerName(a.provider_id)}</td>}
                  <td><span className={`badge ${a.status}`}>{a.status}</span></td>
                  <td>{a.reason ?? ""}</td>
                  <td><Link to={`/bookings/${a.id}`}>view</Link></td>
                  <td>
                    {role && canCancel(a.status, role) && (
                      cancelling === a.id
                        ? <CancelForm appointment={a} onDone={() => setCancelling(null)} />
                        : <button onClick={() => setCancelling(a.id)}>Cancel</button>
                    )}
                  </td>
                </tr>
              ))}
              {query.data.items.length === 0 && <tr><td colSpan={columns}>No appointments.</td></tr>}
            </tbody>
          </table>
          <Pager page={query.data} onOffset={setOffset} />
        </>
      )}
    </section>
  );
}
