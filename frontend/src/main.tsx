import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import type { Role } from "./auth";
import { TooltipProvider } from "@/components/ui/tooltip";
import Appointments from "./pages/Appointments";
import AssistantPage from "./pages/Assistant";
import Book from "./pages/Book";
import Reports from "./pages/Reports";
import Usage from "./pages/Usage";
import Booking from "./pages/Booking";
import Login from "./pages/Login";
import Patients from "./pages/Patients";
import Providers from "./pages/Providers";
import Waitlist from "./pages/Waitlist";
import "./index.css";
import "./styles.css";

// Navigation shows only what the role may do; the API still enforces it.
const NAV: { to: string; label: string; roles: Role[] }[] = [
  { to: "/assistant", label: "Assistant", roles: ["patient", "provider", "front_desk", "admin"] },
  { to: "/patients", label: "Patients", roles: ["front_desk", "admin", "provider"] },
  { to: "/providers", label: "Providers", roles: ["patient", "provider", "front_desk", "admin"] },
  { to: "/appointments", label: "Appointments", roles: ["patient", "provider", "front_desk", "admin"] },
  { to: "/book", label: "Book", roles: ["patient", "front_desk", "admin"] },
  { to: "/waitlist", label: "Waitlist", roles: ["patient", "front_desk", "admin"] },
  { to: "/reports", label: "Reports", roles: ["admin"] },
  { to: "/usage", label: "AI usage", roles: ["admin"] },
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
        <Route path="/appointments" element={<Appointments />} />
        <Route path="/book" element={<Book />} />
        <Route path="/bookings/:id" element={<Booking />} />
        <Route path="/waitlist" element={<Waitlist />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/reports" element={<Reports />} />
        {/* One route with an optional segment: opening a thread keeps the page mounted. */}
        <Route path="/assistant/:conversationId?" element={<AssistantPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TooltipProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </TooltipProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
