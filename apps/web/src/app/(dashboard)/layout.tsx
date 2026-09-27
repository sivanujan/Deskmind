"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, setCsrfToken } from "@/lib/api";

const NAV_ITEMS = [
  { href: "/overview", label: "Overview" },
  { href: "/chart", label: "Chart" },
  { href: "/control", label: "Control" },
  { href: "/backtest", label: "Backtest Lab" },
  { href: "/trades", label: "Trades & Journal" },
  { href: "/risk", label: "Risk" },
  { href: "/agents", label: "Agents" },
  { href: "/strategies", label: "Strategies" },
  { href: "/account", label: "Account" },
  { href: "/audit", label: "Audit Log" },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const [tradingMode, setTradingMode] = useState<string>("testnet");
  const [riskState, setRiskState] = useState<string>("normal");
  const [user, setUser] = useState<string | null>(null);
  const [showLoginModal, setShowLoginModal] = useState(false);
  const [usernameInput, setUsernameInput] = useState("admin");
  const [passwordInput, setPasswordInput] = useState("");
  const [loginError, setLoginError] = useState("");
  const [isLoggingIn, setIsLoggingIn] = useState(false);

  useEffect(() => {
    // Initial fetch of status & user
    api.me()
      .then((data) => {
        if (data.authenticated) {
          setUser(data.username);
        }
      })
      .catch(() => {});

    api.systemStatus()
      .then((status) => {
        setTradingMode(status.trading_mode);
        setRiskState(status.risk_state);
      })
      .catch(() => {});

    const interval = setInterval(() => {
      api.systemStatus()
        .then((status) => {
          setTradingMode(status.trading_mode);
          setRiskState(status.risk_state);
        })
        .catch(() => {});
    }, 10000);

    return () => clearInterval(interval);
  }, []);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoginError("");
    setIsLoggingIn(true);
    try {
      const resp = await api.login(usernameInput, passwordInput);
      setUser(resp.username);
      setShowLoginModal(false);
      setPasswordInput("");
    } catch (err: unknown) {
      setLoginError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setIsLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await api.logout();
      setCsrfToken("");
      setUser(null);
    } catch {
      setUser(null);
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      {/* Top Header */}
      <header className="flex h-12 items-center justify-between border-b border-border bg-surface px-4">
        <div className="flex items-center gap-6">
          <Link href="/overview" className="flex items-center gap-2 font-bold tracking-wider text-accent text-base">
            <span className="h-2.5 w-2.5 rounded-full bg-accent animate-pulse" />
            DESKMIND
          </Link>

          {/* Navigation Bar */}
          <nav className="flex items-center gap-1 text-xs font-medium">
            {NAV_ITEMS.map((item) => {
              const active = pathname === item.href || (item.href !== "/overview" && pathname.startsWith(item.href));
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`rounded px-2.5 py-1.5 transition-colors ${
                    active
                      ? "bg-surface-2 text-accent font-semibold border border-border"
                      : "text-muted hover:text-foreground hover:bg-surface-2/50"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>

        {/* Right Status Badges & Auth */}
        <div className="flex items-center gap-3">
          {/* HARD SAFETY MODE BADGE - NEVER LIVE */}
          <span
            className={`inline-flex items-center gap-1.5 rounded border px-2.5 py-1 text-xs font-bold uppercase tracking-wider ${
              tradingMode === "paper"
                ? "border-accent/40 bg-accent/10 text-accent"
                : "border-warn/40 bg-warn/10 text-warn"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                tradingMode === "paper" ? "bg-accent" : "bg-warn"
              }`}
            />
            {tradingMode === "paper" ? "PAPER" : "TESTNET / DEMO"}
          </span>

          {/* Risk State Badge */}
          <span
            className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${
              riskState === "normal"
                ? "border-up/40 bg-up/10 text-up"
                : riskState === "half_risk"
                ? "border-warn/40 bg-warn/10 text-warn"
                : "border-down/40 bg-down/10 text-down font-bold"
            }`}
          >
            {riskState.replace("_", " ").toUpperCase()}
          </span>

          {/* User profile / Login */}
          {user ? (
            <div className="flex items-center gap-2 border-l border-border pl-3 text-xs">
              <span className="text-muted">User:</span>
              <span className="font-semibold text-foreground">{user}</span>
              <button
                onClick={handleLogout}
                className="rounded border border-border px-2 py-0.5 text-xs text-muted hover:text-down hover:border-down/50 transition-colors"
              >
                Logout
              </button>
            </div>
          ) : (
            <button
              onClick={() => setShowLoginModal(true)}
              className="rounded bg-accent/20 border border-accent/40 px-3 py-1 text-xs font-semibold text-accent hover:bg-accent/30 transition-colors"
            >
              Control Login
            </button>
          )}
        </div>
      </header>

      {/* Main Content Area */}
      <main className="flex flex-1 flex-col overflow-auto bg-background">{children}</main>

      {/* Login Modal */}
      {showLoginModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-xs">
          <div className="w-full max-w-sm rounded-lg border border-border bg-surface p-6 shadow-2xl">
            <h2 className="text-lg font-bold text-foreground">Dashboard Authentication</h2>
            <p className="mt-1 text-xs text-muted">
              Enter credentials to authorize control actions and session tokens.
            </p>

            <form onSubmit={handleLogin} className="mt-4 space-y-3">
              <div>
                <label className="block text-xs font-medium text-muted">Username</label>
                <input
                  type="text"
                  value={usernameInput}
                  onChange={(e) => setUsernameInput(e.target.value)}
                  className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:border-accent focus:outline-hidden"
                  required
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-muted">Password</label>
                <input
                  type="password"
                  value={passwordInput}
                  onChange={(e) => setPasswordInput(e.target.value)}
                  placeholder="Enter admin password..."
                  className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-sm text-foreground focus:border-accent focus:outline-hidden"
                  required
                />
              </div>

              {loginError && (
                <div className="rounded border border-down/40 bg-down/10 px-3 py-1.5 text-xs text-down">
                  {loginError}
                </div>
              )}

              <div className="mt-4 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowLoginModal(false)}
                  className="rounded border border-border px-3 py-1.5 text-xs text-muted hover:text-foreground"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isLoggingIn}
                  className="rounded bg-accent px-4 py-1.5 text-xs font-semibold text-white hover:bg-accent/80 disabled:opacity-50"
                >
                  {isLoggingIn ? "Logging in..." : "Login"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
