"use client";

import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { CLOSING_SECTION } from "@/lib/constants";

export function ClosingSection() {
  const sectionRef = useScrollReveal(0.1);

  return (
    <section ref={sectionRef} className="reveal relative px-6 py-28 md:py-40">
      <div className="relative z-10 mx-auto max-w-5xl text-center">
        {/* Pre-headline */}
        <p className="mb-6 text-sm font-medium uppercase tracking-widest text-muted-foreground">
          {CLOSING_SECTION.preHeadline}
        </p>

        {/* Headline with gradient */}
        <h2 className="text-balance text-4xl font-bold tracking-tight md:text-6xl lg:text-7xl">
          <span className="gradient-text">{CLOSING_SECTION.headline}</span>
        </h2>

        {/* Sub-headline */}
        <p className="mx-auto mt-8 max-w-2xl text-pretty text-lg leading-relaxed text-muted-foreground md:text-xl">
          {CLOSING_SECTION.subHeadline}
        </p>

        {/* Decorative dots */}
        <div className="mt-16 flex items-center justify-center gap-2">
          {[...Array(5)].map((_, i) => (
            <div
              key={i}
              className="h-1.5 rounded-full bg-gradient-to-r from-primary to-accent transition-all duration-300"
              style={{ 
                width: i === 2 ? 32 : 6,
                opacity: i === 2 ? 1 : 0.3 + i * 0.1 
              }}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
