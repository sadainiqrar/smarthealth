// Hand-written mirrors of the Pydantic read models (spec F-3).
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
  provider_id: string | null; slot_id: string | null; reason: string | null;
  confirmed_at: string | null; created_at: string | null;
}
export interface BookingAccepted { appointment_id: string; status: string }
