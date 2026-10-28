import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { ErrorText } from "../shared";

export default function Login() {
  const { session, login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const mutation = useMutation({
    mutationFn: () => login(email, password),
    onSuccess: () => navigate("/", { replace: true }),
  });

  if (session) return <Navigate to="/" replace />;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    mutation.mutate();
  };

  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Sign in</h1>
      <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
      <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
      <button type="submit" disabled={mutation.isPending}>Sign in</button>
      <ErrorText error={mutation.error} />
    </form>
  );
}
