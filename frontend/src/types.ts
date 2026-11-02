// The fields the client uses from the Pydantic read models (spec F-3): hand-written,
// not full mirrors -- the API may return more.
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface Patient {
  id: string; mrn: string; first_name: string; last_name: string;
  date_of_birth: string | null; phone: string | null; email: string | null;
}
export interface Provider {
  id: string; first_name: string; last_name: string; specialty: string; is_active: boolean;
}
export interface Slot {
  id: string; provider_id: string; clinic_id: string; department_id: string | null;
  starts_at: string; ends_at: string;
}
export interface Appointment {
  id: string; status: string; in_progress: boolean; patient_id: string | null;
  provider_id: string | null; slot_id: string | null;
  starts_at: string | null; // the slot's start; null until a slot is claimed
  reason: string | null; confirmed_at: string | null; created_at: string | null;
}
export interface BookingAccepted { appointment_id: string; status: string }
export interface RescheduleAccepted { appointment_id: string; rescheduled_from: string }
export interface Visit {
  appointment_id: string; status: "checked_in" | "in_progress" | "completed";
  checked_in_at: string; seen_at: string | null; completed_at: string | null;
}
export interface WaitlistEntry {
  id: string; patient_id: string; provider_id: string | null; department_id: string | null;
  window_start: string; window_end: string; status: string; created_at: string;
}
