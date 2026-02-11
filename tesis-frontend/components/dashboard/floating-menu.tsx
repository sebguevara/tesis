"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogOut, Menu, X } from "lucide-react";
import { SignOutButton } from "@clerk/nextjs";
import { useState } from "react";

export function FloatingMenu() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  const itemClass =
    "rounded-xl border border-border/40 bg-card/80 px-4 py-2 text-sm text-foreground transition-colors hover:border-primary/40 hover:bg-card";
  const activeClass = "border-primary/40 bg-primary/10";

  return (
    <div className="fixed bottom-6 right-6 z-40 flex flex-col items-end gap-3">
      {open ? (
        <div className="glass-strong flex min-w-[180px] flex-col gap-2 rounded-2xl border border-border/40 p-3 shadow-2xl">
          <Link
            href="/dashboard/crawl"
            onClick={() => setOpen(false)}
            className={`${itemClass} ${
              pathname?.startsWith("/dashboard/crawl") ? activeClass : ""
            }`}
          >
            Inicio
          </Link>
          <Link
            href="/dashboard/projects"
            onClick={() => setOpen(false)}
            className={`${itemClass} ${
              pathname?.startsWith("/dashboard/projects") ? activeClass : ""
            }`}
          >
            Proyectos
          </Link>
          <SignOutButton>
            <button
              type="button"
              className={`${itemClass} flex items-center gap-2 text-left`}
            >
              <LogOut className="h-4 w-4" />
              Sign out
            </button>
          </SignOutButton>
        </div>
      ) : null}

      <button
        type="button"
        onClick={() => setOpen((prev) => !prev)}
        className="inline-flex h-12 w-12 items-center justify-center rounded-2xl border border-border/40 bg-card/90 text-foreground shadow-xl transition-colors hover:border-primary/40"
        aria-expanded={open}
        aria-label="Abrir menu de dashboard"
      >
        {open ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
      </button>
    </div>
  );
}
