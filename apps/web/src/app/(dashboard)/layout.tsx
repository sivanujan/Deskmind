import Link from "next/link";

/**
 * Shell for every dashboard page. Authentication will be added here later (a session check
 * in this layout, plus a Next 16 `proxy.ts` for request-level redirects). Until then the app
 * is served on localhost only and the API it calls is read-only.
 */
export default function DashboardLayout({ children }: LayoutProps<"/">) {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex h-11 items-center gap-6 border-b border-border bg-surface px-4">
        <Link href="/chart" className="font-semibold tracking-wide text-foreground">
          Deskmind
        </Link>
        <nav className="flex gap-4 text-sm">
          <Link href="/chart" className="text-foreground hover:text-accent">
            Chart
          </Link>
        </nav>
        <span className="ml-auto rounded border border-border px-2 py-0.5 text-xs text-muted">
          read-only · testnet
        </span>
      </header>
      <main className="flex flex-1 flex-col">{children}</main>
    </div>
  );
}
