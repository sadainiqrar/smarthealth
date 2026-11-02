import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import {
  VISIT_LABELS, canCancel, canReschedule, nextVisitAction,
} from "../appointments";
import type { VisitAction } from "../appointments";
import { useAuth } from "../auth";
import { POLL_MS, isTerminal } from "../poll";
import { ErrorText } from "../shared";
import type { Appointment, Provider, RescheduleAccepted, Slot, Visit } from "../types";
import { CancelForm } from "./Appointments";
import { ProviderPicker, SlotPicker } from "./Book";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");

// A 404 here means "no visit yet" (nobody has checked in), not an error.
async function fetchVisit(id: string): Promise<Visit | null> {
  try {
    return await api<Visit>(`/appointments/${id}/visit`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

function VisitCard({ visit, status }: { visit: Visit | null; status: string }) {
  if (!visit) {
    if (status === "no_show") return <p>Visit: the patient did not arrive (no-show).</p>;
    return status === "confirmed" ? <p>Visit: not checked in yet.</p> : null;
  }
  return (
    <div>
      <p>Visit: <span className={`badge ${visit.status}`}>{visit.status.replace("_", " ")}</span></p>
      <ul className="timeline">
        <li>Checked in: {when(visit.checked_in_at)}</li>
        <li>Seen: {visit.seen_at ? when(visit.seen_at) : "not yet"}</li>
        <li>Completed: {visit.completed_at ? when(visit.completed_at) : "not yet"}</li>
      </ul>
    </div>
  );
}

function VisitButton({ appointmentId, action }: { appointmentId: string; action: VisitAction }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => api<Visit>(`/appointments/${appointmentId}/${action}`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["visit", appointmentId] });
      queryClient.invalidateQueries({ queryKey: ["appointment", appointmentId] });
      queryClient.invalidateQueries({ queryKey: ["appointments"] });
    },
  });
  return (
    <>
      <button type="button" className="primary" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
        {VISIT_LABELS[action]}
      </button>
      <ErrorText error={mutation.error} />
    </>
  );
}

// Book the new slot first, release the old one after (the server's order): the answer is
// a 202 with the NEW appointment's id, which is what to watch.
function RescheduleForm({ appointment, onDone }: { appointment: Appointment; onDone: () => void }) {
  const navigate = useNavigate();
  const [provider, setProvider] = useState<Provider | null>(null);
  const [slot, setSlot] = useState<Slot | null>(null);
  const [reason, setReason] = useState("");
  const last = useRef<{ key: string; body: string } | null>(null);
  const providerId = provider?.id ?? appointment.provider_id;
  const mutation = useMutation({
    mutationFn: () => {
      const json = { new_slot_id: slot!.id, ...(reason ? { reason } : {}) };
      const body = JSON.stringify(json);
      if (last.current?.body !== body) last.current = { key: crypto.randomUUID(), body };
      return api<RescheduleAccepted>(`/appointments/${appointment.id}/reschedule`, {
        method: "POST", headers: { "Idempotency-Key": last.current.key }, json,
      });
    },
    onSuccess: (accepted) => {
      onDone();
      navigate(`/bookings/${accepted.appointment_id}`);
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };
  return (
    <div className="card">
      <h2>Move to another time</h2>
      <p className="muted">The new time is booked first; the old one is released only if that works.</p>
      {!provider && <p>Showing the current provider's free slots. Pick another provider to change.</p>}
      <ProviderPicker value={provider} onPick={(p) => { setProvider(p); setSlot(null); mutation.reset(); }} />
      {providerId && (
        <SlotPicker providerId={providerId} value={slot} onPick={(s) => { setSlot(s); mutation.reset(); }} />
      )}
      <form onSubmit={submit}>
        <label>Reason (optional)
          <input value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="actions">
          <button type="submit" disabled={!slot || mutation.isPending}>Move appointment</button>
          <button type="button" onClick={onDone}>Keep current time</button>
        </div>
        <ErrorText error={mutation.error} />
      </form>
    </div>
  );
}

export default function Booking() {
  const { id } = useParams();
  const { session } = useAuth();
  const role = session?.role;
  const [panel, setPanel] = useState<"none" | "cancel" | "move">("none");
  const query = useQuery({
    queryKey: ["appointment", id],
    queryFn: () => api<Appointment>(`/appointments/${id}`),
    // Poll on status, never on in_progress (see poll.ts).
    // A 4xx (not found, forbidden) will not fix itself; 5xx/503 may, so keep polling those.
    refetchInterval: (q) => {
      if (q.state.error instanceof ApiError && q.state.error.status < 500) return false;
      return q.state.data && isTerminal(q.state.data.status) ? false : POLL_MS;
    },
  });
  const appt = query.data;
  const visitQuery = useQuery({
    queryKey: ["visit", id],
    queryFn: () => fetchVisit(id!),
    enabled: !!appt && appt.status !== "pending",
  });
  const visit = visitQuery.data ?? null;
  const visitKnown = visitQuery.isSuccess;
  const action = appt && role && visitKnown ? nextVisitAction(appt.status, visit, role) : null;

  return (
    <section>
      <h1>Appointment</h1>
      <ErrorText error={query.error} />
      {appt && (
        <>
          <div className="card">
            <p>Status: <span className={`badge ${appt.status}`}>{appt.status}</span></p>
            {appt.starts_at && <p>Appointment time: {when(appt.starts_at)}</p>}
            {appt.confirmed_at && <p>Confirmed at {when(appt.confirmed_at)}</p>}
            {appt.reason && <p>Reason: {appt.reason}</p>}
            <p className="muted">Appointment <code>{appt.id}</code>
              {appt.slot_id && <> · slot <code>{appt.slot_id}</code></>}</p>
            {appt.status === "failed" && (
              <p className="error">Booking failed - <Link to="/book">choose another slot</Link></p>
            )}
            {appt.status === "rescheduled" && (
              <p>This appointment was moved. <Link to="/appointments">See the new one in Appointments</Link>.</p>
            )}
          </div>

          {(appt.status === "confirmed" || appt.status === "no_show" || visit !== null) && (
            <div className="card">
              <ErrorText error={visitQuery.error} />
              {visitKnown && <VisitCard visit={visit} status={appt.status} />}
              {action && <div className="actions"><VisitButton appointmentId={appt.id} action={action} /></div>}
            </div>
          )}

          {role && panel === "none" && (
            <div className="actions">
              {visitKnown && canReschedule(appt.status, role, visit) && (
                <button type="button" onClick={() => setPanel("move")}>Move to another time</button>
              )}
              {/* A checked-in appointment cannot be cancelled (require_cancellable). */}
              {visitKnown && visit === null && canCancel(appt.status, role) && (
                <button type="button" onClick={() => setPanel("cancel")}>Cancel appointment</button>
              )}
            </div>
          )}
          {panel === "move" && <RescheduleForm appointment={appt} onDone={() => setPanel("none")} />}
          {panel === "cancel" && (
            <div className="card">
              <h2>Cancel appointment</h2>
              <CancelForm appointment={appt} onDone={() => setPanel("none")} />
            </div>
          )}
        </>
      )}
    </section>
  );
}
