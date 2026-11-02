import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { localInputToIso } from "../appointments";
import { useAuth } from "../auth";
import { ErrorText, PAGE_SIZE, Pager } from "../shared";
import type { Page, Patient, Provider, WaitlistEntry } from "../types";
import { PatientPicker, ProviderPicker, label } from "./Book";

const when = (iso: string) => new Date(iso).toLocaleString();

// Staff only (a patient's list is their own). Ids are time-ordered UUIDv7, so a prefix tells
// patients apart poorly; show the name. Cached per patient, so a long list asks once each.
function PatientName({ id }: { id: string }) {
  const query = useQuery({
    queryKey: ["patient", id],
    queryFn: () => api<Patient>(`/patients/${id}`),
    staleTime: 60_000,
  });
  return <>{query.data ? `${label(query.data)} (${query.data.mrn})` : "..."}</>;
}

// When a slot frees up (a cancel or a move), up to three matching active entries are
// offered it, oldest first: the provider must match if one is named, and the slot must
// start inside the window.
function JoinForm() {
  const { session } = useAuth();
  const isPatient = session?.role === "patient";
  const queryClient = useQueryClient();
  const own = useQuery({
    queryKey: ["patients", "me"],
    queryFn: () => api<Patient>("/patients/me"),
    enabled: isPatient,
  });
  const [chosen, setChosen] = useState<Patient | null>(null);
  const [provider, setProvider] = useState<Provider | null>(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const patient = isPatient ? own.data ?? null : chosen;
  const mutation = useMutation({
    mutationFn: () => api<WaitlistEntry>("/waitlist", {
      method: "POST",
      json: {
        patient_id: patient!.id,
        ...(provider ? { provider_id: provider.id } : {}),
        window_start: localInputToIso(start),
        window_end: localInputToIso(end),
      },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["waitlist"] });
      setStart(""); setEnd(""); setProvider(null);
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };
  return (
    <div className="card">
      <h2>Join the waitlist</h2>
      <PatientPicker value={chosen} onPick={(p) => { setChosen(p); mutation.reset(); }} />
      <ProviderPicker value={provider} onPick={(p) => { setProvider(p); mutation.reset(); }} />
      {provider && <button type="button" onClick={() => setProvider(null)}>Any provider</button>}
      <form onSubmit={submit}>
        <div className="row">
          <label>Earliest start
            <input type="datetime-local" value={start} required onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>Latest start
            <input type="datetime-local" value={end} required onChange={(e) => setEnd(e.target.value)} />
          </label>
        </div>
        <button type="submit" disabled={!patient || !start || !end || mutation.isPending}>Add to waitlist</button>
        <ErrorText error={mutation.error} />
        {mutation.isSuccess && <p>Added. If a matching slot frees up, it will be booked automatically.</p>}
      </form>
    </div>
  );
}

export default function Waitlist() {
  const { session } = useAuth();
  const isPatient = session?.role === "patient";
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["waitlist", offset],
    queryFn: () => api<Page<WaitlistEntry>>(`/waitlist?limit=${PAGE_SIZE}&offset=${offset}`),
  });
  const providers = useQuery({
    queryKey: ["providers", "picker"],
    queryFn: () => api<Page<Provider>>("/providers?limit=100"),
  });
  const providerName = (id: string | null) => {
    if (!id) return "any";
    const p = providers.data?.items.find((x) => x.id === id);
    return p ? label(p) : id.slice(0, 8);
  };
  return (
    <section>
      <h1>{isPatient ? "My waitlist" : "Waitlist"}</h1>
      <ErrorText error={query.error} />
      {query.data && (
        <>
          <table>
            <thead>
              <tr>
                {!isPatient && <th>Patient</th>}<th>Provider</th><th>Window</th><th>Status</th><th>Added</th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((w) => (
                <tr key={w.id}>
                  {!isPatient && <td><PatientName id={w.patient_id} /></td>}
                  <td>{providerName(w.provider_id)}</td>
                  <td>{when(w.window_start)} - {when(w.window_end)}</td>
                  <td><span className={`badge ${w.status}`}>{w.status}</span></td>
                  <td>{when(w.created_at)}</td>
                </tr>
              ))}
              {query.data.items.length === 0 && (
                <tr><td colSpan={isPatient ? 4 : 5}>Nobody is waiting.</td></tr>
              )}
            </tbody>
          </table>
          <Pager page={query.data} onOffset={setOffset} />
        </>
      )}
      <JoinForm />
    </section>
  );
}
