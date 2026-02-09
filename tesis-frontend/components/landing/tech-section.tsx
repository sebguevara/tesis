"use client";

import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { TECH_SECTION } from "@/lib/constants";

export function TechSection() {
  const sectionRef = useScrollReveal(0.1);
  const gridRef = useScrollReveal(0.05);

  return (
    <section id="tech" className="relative px-6 py-28 md:py-36 overflow-hidden">
      {/* Subtle background shift */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-warm-100/50 to-transparent" />

      <div className="relative z-10 mx-auto max-w-7xl">
        <div ref={sectionRef} className="reveal mb-20 max-w-3xl">
          <span className="mb-5 inline-flex items-center gap-2 rounded-full glass px-4 py-2 text-xs font-semibold uppercase tracking-wider text-warm-600">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.384 3.065A1.5 1.5 0 014.5 17.052V6.948a1.5 1.5 0 011.536-1.183l5.384 3.065m0 6.34V8.83m0 6.34l5.384 3.065A1.5 1.5 0 0019.5 17.052V6.948a1.5 1.5 0 00-1.536-1.183L12.58 8.83" />
            </svg>
            {TECH_SECTION.label}
          </span>
          <h2 className="text-balance text-3xl font-bold tracking-tight text-foreground md:text-5xl lg:text-6xl">
            {TECH_SECTION.title}
          </h2>
        </div>

        <div ref={gridRef} className="stagger-children grid gap-4 md:grid-cols-2 lg:grid-cols-3">
          {TECH_SECTION.specs.map((spec, i) => (
            <div
              key={i}
              className="glow-card glass rounded-2xl p-6 group"
            >
              {/* Label */}
              <div className="mb-3 flex items-center gap-2">
                <div className="h-1.5 w-1.5 rounded-full bg-gradient-to-r from-primary to-accent" />
                <span className="font-mono text-xs font-medium uppercase tracking-wider text-warm-500">
                  {spec.label}
                </span>
              </div>

              <p className="text-sm leading-relaxed text-foreground">
                {spec.value}
              </p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
