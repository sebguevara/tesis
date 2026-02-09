"use client";

import { DASHBOARD } from "@/lib/constants";
import type { CrawlState } from "@/app/dashboard/page";

interface CrawlStatusProps {
  crawlState: CrawlState;
}

function formatEta(seconds: number): string {
  if (seconds <= 0) return "...";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `${s}s`;
  return `${m}m ${s}s`;
}

export function CrawlStatus({ crawlState }: CrawlStatusProps) {
  const phaseLabel =
    DASHBOARD.crawlStatus.phases[crawlState.phase] || crawlState.phase;

  return (
    <div className="flex min-h-[75vh] flex-col items-center justify-center">
      <div className="w-full max-w-2xl">
        {/* Header */}
        <div className="mb-10 text-center">
          {/* Animated spinner icon */}
          <div className="mx-auto mb-8 relative">
            <div className="flex h-20 w-20 mx-auto items-center justify-center rounded-3xl bg-gradient-to-br from-primary/20 to-accent/20 backdrop-blur-sm border border-primary/10">
              <svg
                className="h-10 w-10 text-primary spin-slow"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182M16.023 9.348h4.992"
                />
              </svg>
            </div>
            {/* Orbiting ring */}
            <div className="absolute inset-0 mx-auto h-20 w-20 rounded-3xl border-2 border-primary/10 border-t-primary/40 spin-slow" />
          </div>

          <h2 className="mb-3 text-2xl font-bold tracking-tight text-foreground md:text-3xl">
            {DASHBOARD.crawlStatus.title}
          </h2>
          <p className="mx-auto max-w-md text-sm leading-relaxed text-muted-foreground">
            {DASHBOARD.crawlStatus.message}
          </p>
        </div>

        {/* Main status card */}
        <div className="glass glow-card rounded-3xl p-1">
          <div className="rounded-[22px] bg-card/80 p-8">
            {/* URL & Phase */}
            <div className="mb-8 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground/50">Dominio</p>
                <p className="mt-1 font-mono text-sm text-foreground">
                  {crawlState.url}
                </p>
              </div>
              <span className="shimmer inline-flex items-center gap-2 self-start rounded-full bg-gradient-to-r from-primary/15 to-accent/15 px-4 py-1.5 text-xs font-semibold text-primary">
                <span className="h-2 w-2 rounded-full bg-gradient-to-r from-primary to-accent progress-pulse" />
                {phaseLabel}
              </span>
            </div>

            {/* Progress bar */}
            <div className="mb-8">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-xs text-muted-foreground">{crawlState.message}</span>
                <span className="font-mono text-sm font-semibold text-primary">{Math.round(crawlState.progressPct)}%</span>
              </div>
              <div className="h-3 w-full overflow-hidden rounded-full bg-warm-200/50 backdrop-blur-sm">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-primary via-accent to-primary bg-[length:200%_100%] transition-all duration-1000 ease-out"
                  style={{ 
                    width: `${crawlState.progressPct}%`,
                    animation: "gradient-text-flow 3s ease infinite",
                  }}
                />
              </div>
            </div>

            {/* Stats grid */}
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              {[
                { label: "Paginas rastreadas", value: crawlState.pagesCrawled },
                { label: "Tiempo estimado", value: formatEta(crawlState.etaSeconds), isMono: true },
                ...(crawlState.metrics
                  ? [
                      { label: "Docs guardados", value: crawlState.metrics.saved_docs },
                      {
                        label: "Contenido filtrado",
                        value: crawlState.metrics.blocked_by_host_filter + crawlState.metrics.blocked_by_block_filter,
                      },
                    ]
                  : []),
              ].map((stat, i) => (
                <div key={i} className="glass rounded-2xl p-4 transition-all hover:scale-[1.02]">
                  <p className="text-[11px] font-medium uppercase tracking-wider text-muted-foreground/50">{stat.label}</p>
                  <p className={`mt-2 text-2xl font-bold text-foreground count-in ${typeof stat.value === "string" ? "font-mono text-lg" : ""}`}>
                    {stat.value}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Job ID */}
        <p className="mt-6 text-center font-mono text-[11px] text-muted-foreground/30">
          Job ID: {crawlState.jobId}
        </p>
      </div>
    </div>
  );
}
