"use client";

import { useState, useEffect } from "react";
import { DASHBOARD, WIDGET_SNIPPET } from "@/lib/constants";
import type { CrawlState } from "@/app/dashboard/page";
import { IntegrationGuide } from "./integration-guide";

interface WidgetResultProps {
  crawlState: CrawlState;
  onReset: () => void;
}

export function WidgetResult({ crawlState, onReset }: WidgetResultProps) {
  const [copied, setCopied] = useState(false);
  const [mounted, setMounted] = useState(false);
  const snippet = WIDGET_SNIPPET(crawlState.jobId);

  useEffect(() => {
    setMounted(true);
  }, []);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = snippet;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <div
      className={`space-y-12 transition-all duration-700 ${
        mounted ? "opacity-100 translate-y-0" : "opacity-0 translate-y-8"
      }`}
    >
      {/* Success header */}
      <div className="text-center">
        <div className="mx-auto mb-8 relative">
          <div className="flex h-20 w-20 mx-auto items-center justify-center rounded-3xl bg-gradient-to-br from-green-400/20 to-emerald-400/20 backdrop-blur-sm border border-green-400/20">
            <svg
              className="h-10 w-10 text-green-500"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
          </div>
        </div>
        <h2 className="mb-3 text-3xl font-bold tracking-tight text-foreground md:text-4xl">
          {DASHBOARD.widgetReady.title}
        </h2>
        <p className="text-muted-foreground">
          {DASHBOARD.widgetReady.subtitle}
        </p>
      </div>

      {/* Stats summary */}
      <div className="mx-auto grid max-w-2xl grid-cols-3 gap-4">
        {[
          { value: crawlState.pagesCrawled, label: "Paginas indexadas" },
          { value: crawlState.metrics?.saved_docs ?? 0, label: "Documentos procesados" },
          { value: "1", label: "Widget generado", isAccent: true },
        ].map((stat, i) => (
          <div
            key={i}
            className="glass glow-card rounded-2xl p-5 text-center"
          >
            <p className={`font-mono text-3xl font-bold count-in ${stat.isAccent ? "gradient-text" : "text-foreground"}`}>
              {stat.value}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">{stat.label}</p>
          </div>
        ))}
      </div>

      {/* Code snippet */}
      <div className="mx-auto max-w-2xl">
        <div className="glass glow-card overflow-hidden rounded-3xl p-1">
          <div className="rounded-[20px] overflow-hidden">
            {/* Header */}
            <div className="flex items-center justify-between bg-warm-800 px-5 py-3.5">
              <div className="flex items-center gap-3">
                <div className="flex gap-1.5">
                  <div className="h-3 w-3 rounded-full bg-warm-600/60" />
                  <div className="h-3 w-3 rounded-full bg-warm-500/60" />
                  <div className="h-3 w-3 rounded-full bg-warm-400/60" />
                </div>
                <span className="font-mono text-xs text-warm-400">snippet.html</span>
              </div>
              <button
                type="button"
                onClick={handleCopy}
                className="flex items-center gap-2 rounded-lg bg-warm-700 px-4 py-2 text-xs font-medium text-warm-100 transition-all hover:bg-warm-600 hover:scale-105 active:scale-95"
              >
                {copied ? (
                  <>
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                    </svg>
                    Copiado
                  </>
                ) : (
                  <>
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9.75a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184" />
                    </svg>
                    Copiar
                  </>
                )}
              </button>
            </div>
            {/* Code */}
            <pre className="overflow-x-auto bg-warm-900 p-6 font-mono text-sm leading-relaxed text-warm-200">
              <code>{snippet}</code>
            </pre>
          </div>
        </div>
      </div>

      {/* Integration guides */}
      <div className="mx-auto max-w-2xl">
        <h3 className="mb-6 text-xl font-semibold tracking-tight text-foreground">
          Guia de integracion paso a paso
        </h3>
        <div className="space-y-3">
          {DASHBOARD.integrationGuides.map((guide) => (
            <IntegrationGuide key={guide.platform} guide={guide} />
          ))}
        </div>
      </div>

      {/* New project button */}
      <div className="flex justify-center pb-10">
        <button
          type="button"
          onClick={onReset}
          className="glass glow-card rounded-2xl px-8 py-4 text-sm font-semibold text-foreground transition-all hover:scale-[1.02] active:scale-[0.98]"
        >
          Crear otro proyecto
        </button>
      </div>
    </div>
  );
}
