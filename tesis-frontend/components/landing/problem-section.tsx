"use client";

import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { PROBLEM_SECTION } from "@/lib/constants";

export function ProblemSection() {
  const sectionRef = useScrollReveal(0.1);
  const cardsRef = useScrollReveal(0.05);

  return (
    <section id="problema" className="relative px-6 py-28 md:py-36">
      <div className="mx-auto max-w-7xl">
        <div ref={sectionRef} className="reveal mb-20 max-w-3xl">
          <span className="mb-5 inline-flex items-center gap-2 rounded-full glass px-4 py-2 text-xs font-semibold uppercase tracking-wider text-warm-600">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            {PROBLEM_SECTION.label}
          </span>
          <h2 className="text-balance text-3xl font-bold tracking-tight text-foreground md:text-5xl lg:text-6xl">
            {PROBLEM_SECTION.title}
          </h2>
          <p className="mt-6 text-lg leading-relaxed text-muted-foreground md:text-xl">
            {PROBLEM_SECTION.description}
          </p>
        </div>

        <div ref={cardsRef} className="stagger-children grid gap-6 md:grid-cols-3">
          {PROBLEM_SECTION.painPoints.map((point, i) => (
            <div
              key={i}
              className="glow-card glass rounded-3xl p-8 group"
            >
              {/* Number with gradient */}
              <div className="mb-6 flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-warm-200 to-warm-100">
                <span className="font-mono text-lg font-bold text-warm-600">
                  {String(i + 1).padStart(2, "0")}
                </span>
              </div>

              <h3 className="mb-3 text-lg font-semibold text-foreground tracking-tight">
                {point.title}
              </h3>
              <p className="text-sm leading-relaxed text-muted-foreground">
                {point.description}
              </p>

              {/* Decorative bottom line */}
              <div className="mt-6 h-1 w-12 rounded-full bg-gradient-to-r from-primary/30 to-accent/30 transition-all duration-500 group-hover:w-full" />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
