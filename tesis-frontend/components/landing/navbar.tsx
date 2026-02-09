"use client";

import Link from "next/link";
import { BRAND } from "@/lib/constants";
import { useEffect, useState } from "react";

export function Navbar() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    function onScroll() {
      setScrolled(window.scrollY > 20);
    }
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <header
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-500 ${
        scrolled
          ? "glass-strong shadow-sm"
          : "bg-transparent"
      }`}
    >
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
        <Link href="/" className="flex items-center gap-3 group">
          <div className="relative flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-primary to-accent overflow-hidden">
            <span className="relative z-10 text-sm font-bold text-primary-foreground">4g</span>
            <div className="absolute inset-0 bg-gradient-to-br from-primary to-accent opacity-0 group-hover:opacity-100 transition-opacity" />
          </div>
          <span className="text-lg font-semibold text-foreground tracking-tight">
            {BRAND.name}
          </span>
        </Link>

        <div className="hidden items-center gap-8 md:flex">
          {[
            { href: "#problema", label: "Desafio actual" },
            { href: "#solucion", label: "Demo del flujo" },
            { href: "#capacidades", label: "Impacto operativo" },
            { href: "#tech", label: "Motor crawler IA" },
          ].map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="relative text-sm text-muted-foreground transition-colors hover:text-foreground group"
            >
              {link.label}
              <span className="absolute -bottom-1 left-0 h-[2px] w-0 bg-gradient-to-r from-primary to-accent transition-all duration-300 group-hover:w-full" />
            </a>
          ))}
        </div>

        <Link
          href="/dashboard"
          className="rounded-xl bg-gradient-to-r from-primary to-accent px-5 py-2.5 text-sm font-medium text-primary-foreground transition-all hover:shadow-lg hover:shadow-primary/20 hover:scale-[1.02] active:scale-[0.98]"
        >
          Ver dashboard
        </Link>
      </nav>
    </header>
  );
}
