import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { ErrorText, Pager, listPath, useDebounced } from "../shared";
import type { Page, Patient } from "../types";

const EMPTY = { mrn: "", first_name: "", last_name: "", date_of_birth: "", phone: "", email: "" };
type Form = typeof EMPTY;

function RegisterForm() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<Form>(EMPTY);
  const mutation = useMutation({
    // Optional fields left blank are omitted, not sent as "" (the API would reject "" as an email).
    mutationFn: () => api<Patient>("/patients", {
      method: "POST",
      json: Object.fromEntries(Object.entries(form).filter(([, v]) => v !== "")),
    }),
    onSuccess: () => {
      setForm(EMPTY);
      queryClient.invalidateQueries({ queryKey: ["patients"] });
    },
  });
  const field = (name: keyof Form, label: string, type = "text", required = false) => (
    <label>{label}
      <input
        type={type} value={form[name]} required={required}
        onChange={(e) => setForm({ ...form, [name]: e.target.value })}
      />
    </label>
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <form className="card" onSubmit={submit}>
      <h2>Register patient</h2>
      <div className="row">
        {field("mrn", "MRN", "text", true)}
        {field("first_name", "First name", "text", true)}
        {field("last_name", "Last name", "text", true)}
      </div>
      <div className="row">
        {field("date_of_birth", "Date of birth", "date")}
        {field("phone", "Phone")}
        {field("email", "Email", "email")}
      </div>
      <button type="submit" disabled={mutation.isPending}>Register</button>
      <ErrorText error={mutation.error} />
    </form>
  );
}

export default function Patients() {
  const { session } = useAuth();
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const term = useDebounced(search);
  const query = useQuery({
    queryKey: ["patients", term, offset],
    queryFn: () => api<Page<Patient>>(listPath("/patients", term, offset)),
  });
  const canRegister = session?.role === "front_desk" || session?.role === "admin";

  return (
    <section>
      <h1>Patients</h1>
      {canRegister && <RegisterForm />}
      <input
        type="search" placeholder="Search patients" value={search}
        onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
      />
      <ErrorText error={query.error} />
      {query.data && (
        <>
          <table>
            <thead><tr><th>MRN</th><th>Name</th><th>DOB</th><th>Phone</th></tr></thead>
            <tbody>
              {query.data.items.map((p) => (
                <tr key={p.id}>
                  <td>{p.mrn}</td><td>{p.first_name} {p.last_name}</td>
                  <td>{p.date_of_birth ?? ""}</td><td>{p.phone ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager page={query.data} onOffset={setOffset} />
        </>
      )}
    </section>
  );
}
