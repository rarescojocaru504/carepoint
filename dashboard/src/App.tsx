import { type FormEvent, useEffect, useState } from "react";

import { type Me, errorMessage, me, refreshSession, signIn, signOut } from "./api";
import { LiveProvider, useLiveConnected } from "./live";
import { MachineView } from "./views/MachineView";
import { Overview } from "./views/Overview";
import { Products } from "./views/Products";
import { Reservations } from "./views/Reservations";

type Route = { page: "overview" } | { page: "machine"; id: string } | { page: "reservations" } | { page: "products" };

function parseRoute(hash: string): Route {
  const machine = /^#\/machines\/([0-9a-f-]{36})$/.exec(hash);
  if (machine?.[1]) return { page: "machine", id: machine[1] };
  if (hash === "#/reservations") return { page: "reservations" };
  if (hash === "#/products") return { page: "products" };
  return { page: "overview" };
}

function useRoute(): [Route, (hash: string) => void] {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = (): void => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return [route, (hash) => (window.location.hash = hash)];
}

export function App() {
  const [user, setUser] = useState<Me | null>(null);
  const [restoring, setRestoring] = useState(true);

  // A reload keeps the session: swap the stored refresh token for a new pair.
  useEffect(() => {
    refreshSession()
      .then((ok) => (ok ? me().then(setUser) : undefined))
      .catch(() => undefined)
      .finally(() => setRestoring(false));
  }, []);

  const logout = (): void => {
    signOut();
    setUser(null);
  };

  if (restoring) return <div className="center muted">Loading…</div>;
  if (user === null) return <Login onSignedIn={setUser} />;
  if (user.role === "customer") return <NoAccess user={user} logout={logout} />;
  return (
    <LiveProvider>
      <Shell user={user} logout={logout} />
    </LiveProvider>
  );
}

function Shell({ user, logout }: { user: Me; logout: () => void }) {
  const [route, go] = useRoute();
  const live = useLiveConnected();
  const nav: [Route["page"], string, string][] = [
    ["overview", "#/", "Overview"],
    ["reservations", "#/reservations", "Reservations"],
    ["products", "#/products", "Products"],
  ];
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          Care<span>Point</span>
          <small>Operations</small>
        </div>
        <nav>
          {nav.map(([page, hash, label]) => (
            <a key={page} href={hash}
              className={route.page === page || (page === "overview" && route.page === "machine") ? "active" : undefined}>
              {label}
            </a>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`live ${live ? "on" : "off"}`} data-testid="live">
            {live ? "Live" : "Reconnecting…"}
          </span>
          <div className="small">{user.email}</div>
          <div className="muted small">{user.role}</div>
          <button type="button" className="link" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        {route.page === "overview" && <Overview open={(id) => go(`#/machines/${id}`)} />}
        {route.page === "machine" && <MachineView machineId={route.id} back={() => go("#/")} />}
        {route.page === "reservations" && <Reservations />}
        {route.page === "products" && <Products />}
      </main>
    </div>
  );
}

function Login({ onSignedIn }: { onSignedIn: (user: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(email.trim(), password));
    } catch (e: unknown) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="center">
      <form className="login card" onSubmit={(e) => void submit(e)} aria-label="Sign in">
        <div className="brand large">
          Care<span>Point</span>
        </div>
        <h1>Operations dashboard</h1>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" required />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" className="button primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

function NoAccess({ user, logout }: { user: Me; logout: () => void }) {
  return (
    <div className="center">
      <div className="login card" role="alert">
        <h1>No access</h1>
        <p>
          {user.email} is a customer account. The operations dashboard is for CarePoint operators
          and admins.
        </p>
        <button type="button" className="button" onClick={logout}>
          Sign in with another account
        </button>
      </div>
    </div>
  );
}
