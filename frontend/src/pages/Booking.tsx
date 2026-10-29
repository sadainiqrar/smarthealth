import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import { POLL_MS, isTerminal } from "../poll";
import { ErrorText } from "../shared";
import type { Appointment } from "../types";

export default function Booking() {
  const { id } = useParams();
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

  return (
    <section>
      <h1>Booking</h1>
      <ErrorText error={query.error} />
      {appt && (
        <div className="card">
          <p>Status: <span className={`badge ${appt.status}`}>{appt.status}</span></p>
          {appt.confirmed_at && <p>Confirmed at {new Date(appt.confirmed_at).toLocaleString()}</p>}
          {appt.starts_at && <p>Appointment time: {new Date(appt.starts_at).toLocaleString()}</p>}
          {appt.slot_id && <p>Slot: <code>{appt.slot_id}</code></p>}
          {appt.reason && <p>Reason: {appt.reason}</p>}
          {appt.status === "failed" && (
            <p className="error">Booking failed - <Link to="/book">choose another slot</Link></p>
          )}
        </div>
      )}
    </section>
  );
}
