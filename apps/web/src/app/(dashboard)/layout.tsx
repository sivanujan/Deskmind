"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, setCsrfToken } from "@/lib/api";

// Navigation grouped by what the operator is doing: watching the market, trading, researching.
const NAV_GROUPS: Array<{ label: string; items: Array<{ href: string; label: string }> }> = [
  {
    label: "Monitor",
    items: [
      { href: "/overview", label: "Overview" },
      { href: "/chart", label: "Chart" },
      { href: "/agents", label: "Agents" },
      { href: "/universe", label: "Universe" },
    ],
  },
  {
    label: "Trade",
    items: [
      { href: "/control", label: "Control" },
      { href: "/trades", label: "Trades & Journal" },
      { href: "/risk", label: "Risk" },
      { href: "/account", label: "Account" },
    ],
  },
  {
    label: "Research",
    items: [
      { href: "/strategy-lab", label: "Strategy Lab" },
      { href: "/backtest", label: "Backtest Lab" },
      { href: "/research", label: "Hypotheses" },
      { href: "/strategies", label: "Strategies" },
    ],
  },
  {
    label: "System",
    items: [{ href: "/audit", label: "Audit Log" }],
  },
];

function isActive(pathname: string, href: string) {
  return pathname === href || (href !== "/overview" && pathname.startsWith(href));
}

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const [tradingMode, setTradingMode] = useState<string | null>(null);
  const [riskState, setRiskState] = useState<string | null>(null);
  const [user, setUser] = useState<string | null>(null);
  const [showLoginModal, setShowLoginModal] = useState(false);
  const [usernameInput, setUsernameInput] = useState("admin");
  const [passwordInput, setPasswordInput] = useState("");
  const [loginError, setLoginError] = useState("");
  const [isLoggingIn, setIsLoggingIn] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    // Initial fetch of status & user
    api.me()
      .then((data) => {
        if (data.authenticated) {
          setUser(data.username);
        }
      })
      .catch(() => {});

    const updateStatus = (status: { trading_mode: string; risk_state: string | null }) => {
      setTradingMode(status.trading_mode);
      setRiskState(status.risk_state); // null = unknown, shown as such
    };

    api.systemStatus().then(updateStatus).catch(() => {});

    const interval = setInterval(() => {
      if (document.hidden) return; // no polling from a background tab
      api.systemStatus().then(updateStatus).catch(() => {});
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

  // HARD SAFETY MODE - NEVER LIVE. Rail colour: paper = accent, testnet = warn, unknown = muted.
  const modeLabel = tradingMode === null ? "Unknown" : tradingMode === "paper" ? "Paper" : "Testnet / demo";
  const modeColor = tradingMode === null ? "var(--muted)" : tradingMode === "paper" ? "var(--accent)" : "var(--warn)";
  const modeText = tradingMode === null ? "text-muted" : tradingMode === "paper" ? "text-accent" : "text-warn";

  const riskLabel = riskState === null ? "Unknown" : riskState.replace(/_/g, " ");
  const riskText =
    riskState === null ? "text-muted" : riskState === "normal" ? "text-up" : riskState === "half_risk" ? "text-warn" : "text-down";
  const riskDot =
    riskState === null ? "bg-muted" : riskState === "normal" ? "bg-up" : riskState === "half_risk" ? "bg-warn" : "bg-down";

  const sidebar = (
    <div className="flex h-full flex-col">
      <Link href="/overview" className="flex items-baseline gap-2 px-5 pt-5 pb-4">
        <span className="font-display text-lg font-bold tracking-[0.18em] text-foreground">DESKMIND</span>
      </Link>

      {/* Desk state: what environment this is and whether the risk manager allows trading. */}
      <div className="mx-3 mb-4 rounded-md border border-border bg-background/60 px-3 py-2.5">
        <div className="flex items-center justify-between">
          <span className="label-caps">Mode</span>
          <span className={`font-display text-xs font-bold uppercase tracking-wider ${modeText}`}>{modeLabel}</span>
        </div>
        <div className="mt-1.5 flex items-center justify-between">
          <span className="label-caps">Risk</span>
          <span className={`flex items-center gap-1.5 font-display text-xs font-bold uppercase tracking-wider ${riskText}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${riskDot}`} />
            {riskLabel}
          </span>
        </div>
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4" aria-label="Main">
        {NAV_GROUPS.map((group) => (
          <div key={group.label}>
            <div className="label-caps px-2 pb-1.5">{group.label}</div>
            <ul className="space-y-0.5">
              {group.items.map((item) => {
                const active = isActive(pathname, item.href);
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      aria-current={active ? "page" : undefined}
                      onClick={() => setNavOpen(false)}
                      className={`relative flex items-center rounded-md px-2 py-1.5 text-[13px] transition-colors ${
                        active
                          ? "bg-surface-2 font-semibold text-foreground"
                          : "text-muted hover:bg-surface-2/60 hover:text-foreground"
                      }`}
                    >
                      {active && <span className="absolute -left-3 top-1 bottom-1 w-[3px] rounded-r bg-accent" />}
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="border-t border-border px-4 py-3 text-xs">
        {user ? (
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-muted">
              Signed in as <span className="font-semibold text-foreground">{user}</span>
            </span>
            <button onClick={handleLogout} className="btn btn-sm">
              Log out
            </button>
          </div>
        ) : (
          <button onClick={() => setShowLoginModal(true)} className="btn btn-primary w-full">
            Log in for control actions
          </button>
        )}
      </div>
    </div>
  );

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <div className="mode-rail sticky top-0 z-40" style={{ ["--rail-color" as string]: modeColor }} aria-hidden />

      {/* Mobile top bar */}
      <header className="flex h-12 items-center justify-between border-b border-border bg-surface px-4 md:hidden">
        <span className="font-display text-base font-bold tracking-[0.18em]">DESKMIND</span>
        <div className="flex items-center gap-3">
          <span className={`font-display text-xs font-bold uppercase tracking-wider ${modeText}`}>{modeLabel}</span>
          <button onClick={() => setNavOpen((o) => !o)} className="btn btn-sm" aria-expanded={navOpen} aria-label="Menu">
            {navOpen ? "Close" : "Menu"}
          </button>
        </div>
      </header>

      <div className="flex flex-1">
        <aside
          className={`${
            navOpen ? "fixed inset-0 top-[52px] z-30 block" : "hidden"
          } w-full shrink-0 border-r border-border bg-surface md:sticky md:top-1 md:block md:h-[calc(100vh-4px)] md:w-56`}
        >
          {sidebar}
        </aside>

        <main className="flex min-w-0 flex-1 flex-col overflow-auto bg-background">{children}</main>
      </div>

      {/* Login Modal */}
      {showLoginModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-xs">
          <div className="w-full max-w-sm rounded-lg border border-border bg-surface p-6 shadow-2xl">
            <h2 className="font-display text-lg font-bold text-foreground">Log in</h2>
            <p className="mt-1 text-xs text-muted">
              Needed for control actions (start/stop services, approvals, uploads). Viewing works without it.
            </p>

            <form onSubmit={handleLogin} className="mt-4 space-y-3">
              <div>
                <label className="label-caps block">Username</label>
                <input
                  type="text"
                  value={usernameInput}
                  onChange={(e) => setUsernameInput(e.target.value)}
                  className="mt-1 w-full px-3 py-1.5 text-sm"
                  required
                />
              </div>

              <div>
                <label className="label-caps block">Password</label>
                <input
                  type="password"
                  value={passwordInput}
                  onChange={(e) => setPasswordInput(e.target.value)}
                  className="mt-1 w-full px-3 py-1.5 text-sm"
                  required
                />
              </div>

              {loginError && (
                <div className="rounded-md border border-down/40 bg-down/10 px-3 py-1.5 text-xs text-down">
                  {loginError}
                </div>
              )}

              <div className="mt-4 flex justify-end gap-2">
                <button type="button" onClick={() => setShowLoginModal(false)} className="btn">
                  Cancel
                </button>
                <button type="submit" disabled={isLoggingIn} className="btn btn-primary">
                  {isLoggingIn ? "Logging in…" : "Log in"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
