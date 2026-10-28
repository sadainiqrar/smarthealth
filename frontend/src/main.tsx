import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import type { Role } from "./auth";
import Book from "./pages/Book";
import Booking from "./pages/Booking";
import Login from "./pages/Login";
import Patients from "./pages/Patients";
import Providers from "./pages/Providers";
import "./styles.css";

// Navigation shows only what the role may do; the API still enforces it.
const NAV: { to: string; label: string; roles: Role[] }[] = [
  { to: "/patients", label: "Patients", roles: ["front_desk", "admin", "provider"] },
  { to: "/providers", label: "Providers", roles: ["patient", "provider", "front_desk", "admin"] },
  { to: "/book", label: "Book", roles: ["patient", "front_desk", "admin"] },
];

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

function Layout() {
  const { session, logout } = useAuth();
  if (!session) return <Navigate to="/login" replace />;
  return (
    <>
      <header className="topbar">
        <strong>SmartHealth</strong>
        <nav>
          {NAV.filter((item) => item.roles.includes(session.role)).map((item) => (
            <NavLink key={item.to} to={item.to}>{item.label}</NavLink>
          ))}
        </nav>
        <span className="who">{session.role}</span>
        <button onClick={logout}>Logout</button>
      </header>
      <main className="container"><Outlet /></main>
    </>
  );
}

function Home() {
  const { session } = useAuth();
  const first = NAV.find((item) => session && item.roles.includes(session.role));
  return <Navigate to={first?.to ?? "/login"} replace />;
}

function App() {
  return (
    <Routes>
      <Route path="/login" element={<main className="container"><Login /></main>} />
      <Route element={<Layout />}>
        <Route path="/" element={<Home />} />
        <Route path="/patients" element={<Patients />} />
        <Route path="/providers" element={<Providers />} />
        <Route path="/book" element={<Book />} />
        <Route path="/bookings/:id" element={<Booking />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
