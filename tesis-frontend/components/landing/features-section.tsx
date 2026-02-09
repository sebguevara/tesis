"use client";

import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { FEATURES_SECTION } from "@/lib/constants";

export function FeaturesSection() {
  const sectionRef = useScrollReveal(0.1);
  const cardsRef = useScrollReveal(0.05);

  return (
    <section id="capacidades" className="relative px-6 py-28 md:py-36">
      <div className="mx-auto max-w-7xl">
        <div ref={sectionRef} className="reveal mb-20 max-w-3xl">
          <span className="mb-5 inline-flex items-center gap-2 rounded-full glass px-4 py-2 text-xs font-semibold uppercase tracking-wider text-warm-600">
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
            </svg>
            {FEATURES_SECTION.label}
          </span>
          <h2 className="text-balance text-3xl font-bold tracking-tight text-foreground md:text-5xl lg:text-6xl">
            {FEATURES_SECTION.title}
          </h2>
        </div>

        <div ref={cardsRef} className="stagger-children grid gap-6 md:grid-cols-2">
          {FEATURES_SECTION.features.map((feature, i) => (
            <div
              key={i}
              className="glow-card glass rounded-3xl p-8 flex flex-col justify-between group"
            >
              <div>
                <h3 className="mb-3 text-lg font-semibold text-foreground tracking-tight">
                  {feature.title}
                </h3>
                <p className="text-sm leading-relaxed text-muted-foreground">
                  {feature.description}
                </p>
              </div>

              {/* Metric section */}
              <div className="mt-8 flex items-baseline gap-3 border-t border-border/50 pt-6">
                <span className="gradient-text font-mono text-4xl font-bold">
                  {feature.metric}
                </span>
                <span className="text-sm text-muted-foreground">
                  {feature.metricLabel}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
