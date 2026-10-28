import { useMutation, useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorText, listPath, useDebounced } from "../shared";
import type { BookingAccepted, Page, Patient, Provider, Slot } from "../types";

const label = (p: { first_name: string; last_name: string }) => `${p.first_name} ${p.last_name}`;

// Staff search for a patient; a patient books for themselves via /patients/me.
function PatientPicker({ value, onPick }: { value: Patient | null; onPick: (p: Patient) => void }) {
  const { session } = useAuth();
  const isPatient = session?.role === "patient";
  const [search, setSearch] = useState("");
  const term = useDebounced(search);
  const own = useQuery({
    queryKey: ["patients", "me"],
    queryFn: () => api<Patient>("/patients/me"),
    enabled: isPatient,
  });
  const found = useQuery({
    queryKey: ["patients", term, 0],
    queryFn: () => api<Page<Patient>>(listPath("/patients", term, 0)),
    enabled: !isPatient,
  });

  if (isPatient) {
    return (
      <div>
        <h2>Patient</h2>
        {own.data && <p>Your record: {label(own.data)} ({own.data.mrn})</p>}
        <ErrorText error={own.error} />
      </div>
    );
  }
  return (
    <div>
      <h2>Patient</h2>
      {value && <p>Selected: <strong>{label(value)}</strong> ({value.mrn})</p>}
      <input type="search" placeholder="Search patients" value={search} onChange={(e) => setSearch(e.target.value)} />
      <ErrorText error={found.error} />
      {found.data?.items.length === 0 && <p>No matches.</p>}
      <ul className="picker">
        {found.data?.items.map((p) => (
          <li key={p.id}>
            <button type="button" className={value?.id === p.id ? "selected" : ""} onClick={() => onPick(p)}>
              {label(p)} ({p.mrn})
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProviderPicker({ value, onPick }: { value: Provider | null; onPick: (p: Provider) => void }) {
  const [search, setSearch] = useState("");
  const term = useDebounced(search);
  const found = useQuery({
    queryKey: ["providers", term, 0],
    queryFn: () => api<Page<Provider>>(listPath("/providers", term, 0)),
  });
  return (
    <div>
      <h2>Provider</h2>
      {value && <p>Selected: <strong>{label(value)}</strong> ({value.specialty})</p>}
      <input type="search" placeholder="Search providers" value={search} onChange={(e) => setSearch(e.target.value)} />
      <ErrorText error={found.error} />
      {found.data?.items.length === 0 && <p>No matches.</p>}
      <ul className="picker">
        {found.data?.items.map((p) => (
          <li key={p.id}>
            <button type="button" className={value?.id === p.id ? "selected" : ""} onClick={() => onPick(p)}>
              {label(p)} - {p.specialty}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SlotPicker({ providerId, value, onPick }: { providerId: string; value: Slot | null; onPick: (s: Slot) => void }) {
  const slots = useQuery({
    queryKey: ["slots", providerId],
    queryFn: () => api<Page<Slot>>(`/slots?provider_id=${providerId}&limit=50`),
  });
  return (
    <div>
      <h2>Free slot</h2>
      <ErrorText error={slots.error} />
      {slots.data && slots.data.total > slots.data.items.length && (
        <p>Showing {slots.data.items.length} of {slots.data.total} free slots.</p>
      )}
      {slots.data?.items.length === 0 && <p>No free slots for this provider.</p>}
      <ul className="picker">
        {slots.data?.items.map((s) => (
          <li key={s.id}>
            <button type="button" className={value?.id === s.id ? "selected" : ""} onClick={() => onPick(s)}>
              {new Date(s.starts_at).toLocaleString()}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export default function Book() {
  const { session } = useAuth();
  const navigate = useNavigate();
  const own = useQuery({
    queryKey: ["patients", "me"],
    queryFn: () => api<Patient>("/patients/me"),
    enabled: session?.role === "patient",
  });
  const [chosen, setChosen] = useState<Patient | null>(null);
  const [provider, setProvider] = useState<Provider | null>(null);
  const [slot, setSlot] = useState<Slot | null>(null);
  const [reason, setReason] = useState("");
  // The server fingerprints patient+slot+reason, so a key is only reusable with an
  // identical body: a Retry click reuses the last attempt's key so the API can dedupe,
  // and any change to the body mints a new one.
  const last = useRef<{ key: string; body: string } | null>(null);

  const patient = session?.role === "patient" ? own.data ?? null : chosen;
  const mutation = useMutation({
    mutationFn: () => {
      const json = { patient_id: patient!.id, slot_id: slot!.id, ...(reason ? { reason } : {}) };
      const body = JSON.stringify(json);
      if (last.current?.body !== body) last.current = { key: crypto.randomUUID(), body };
      return api<BookingAccepted>("/appointments", {
        method: "POST",
        headers: { "Idempotency-Key": last.current.key },
        json,
      });
    },
    onSuccess: (accepted) => navigate(`/bookings/${accepted.appointment_id}`),
  });
  // A new selection is a new booking: drop the previous attempt's error/Retry state.
  const reset = mutation.reset;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <section>
      <h1>Book an appointment</h1>
      <PatientPicker value={chosen} onPick={(p) => { setChosen(p); reset(); }} />
      <ProviderPicker value={provider} onPick={(p) => { setProvider(p); setSlot(null); reset(); }} />
      {provider && <SlotPicker providerId={provider.id} value={slot} onPick={(s) => { setSlot(s); reset(); }} />}
      <form onSubmit={submit}>
        <label>Reason (optional)
          <textarea value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} />
        </label>
        <button type="submit" disabled={!patient || !slot || mutation.isPending}>
          {mutation.isError ? "Retry" : "Book"}
        </button>
        <ErrorText error={mutation.error} />
      </form>
    </section>
  );
}
