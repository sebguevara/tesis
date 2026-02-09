import React from "react";
import Link from "next/link";
import { DASHBOARD } from "@/lib/constants";

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="relative min-h-screen overflow-hidden">
      {/* Dashboard background with subtle animated gradient */}
      <div className="fixed inset-0 animated-gradient-bg" />
      <div className="fixed top-1/4 -left-48 h-[600px] w-[600px] rounded-full bg-gradient-to-br from-primary/8 to-accent/5 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-1/4 -right-48 h-[500px] w-[500px] rounded-full bg-gradient-to-br from-accent/8 to-primary/5 blur-[120px] pointer-events-none" />

      {/* Nav */}
      <header className="relative z-20 glass-strong border-b border-border/30">
        <nav className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <Link href="/" className="flex items-center gap-3 group">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-accent overflow-hidden">
              <span className="text-sm font-bold text-primary-foreground">4g</span>
            </div>
            <span className="text-lg font-semibold text-foreground tracking-tight">
              {DASHBOARD.nav.brand}
            </span>
          </Link>

          <div className="flex items-center gap-6">
            {DASHBOARD.nav.links.map((link) => (
              <Link
                key={link.label}
                href={link.href}
                className="text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                {link.label}
              </Link>
            ))}
            <div className="flex h-9 w-9 items-center justify-center rounded-full bg-gradient-to-br from-primary/20 to-accent/20 text-sm font-semibold text-warm-600 ring-2 ring-primary/10">
              U
            </div>
          </div>
        </nav>
      </header>

      <main className="relative z-10 mx-auto max-w-7xl px-6 py-10">{children}</main>
    </div>
  );
}
