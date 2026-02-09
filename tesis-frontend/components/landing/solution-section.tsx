"use client";

import { useEffect, useState } from "react";
import { useScrollReveal } from "@/hooks/use-scroll-reveal";
import { SOLUTION_SECTION, WIDGET_SNIPPET } from "@/lib/constants";

const DEMO_URL = "https://www.universidad-ejemplo.edu.ar";
const DEMO_JOB_ID = "demo-univ-2026";

const phases = ["URL cargada", "Analisis en curso", "Widget generado"] as const;

export function SolutionSection() {
  const sectionRef = useScrollReveal(0.1);
  const demoRef = useScrollReveal(0.05);

  const [typedUrl, setTypedUrl] = useState("");
  const [progress, setProgress] = useState(0);
  const [activePhase, setActivePhase] = useState(0);
  const [copied, setCopied] = useState(false);
  const [copyAnimating, setCopyAnimating] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const sleep = (ms: number) =>
      new Promise((resolve) => setTimeout(resolve, ms));

    const runDemo = async () => {
      while (!cancelled) {
        setTypedUrl("");
        setProgress(0);
        setActivePhase(0);
        setCopied(false);
        setCopyAnimating(false);

        for (let i = 0; i <= DEMO_URL.length; i += 1) {
          if (cancelled) return;
          setTypedUrl(DEMO_URL.slice(0, i));
          await sleep(42);
        }

        if (cancelled) return;
        setActivePhase(1);

        for (let p = 0; p <= 100; p += 4) {
          if (cancelled) return;
          setProgress(p);
          await sleep(95);
        }

        if (cancelled) return;
        setActivePhase(2);
        await sleep(1700);
      }
    };

    runDemo();

    return () => {
      cancelled = true;
    };
  }, []);

  const pages = Math.round(120 + (progress / 100) * 1240);
  const snippet = WIDGET_SNIPPET(DEMO_JOB_ID);

  async function handleCopySnippet() {
    if (activePhase !== 2) return;
    setCopyAnimating(true);
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = snippet;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
    } finally {
      setTimeout(() => setCopyAnimating(false), 220);
      setTimeout(() => setCopied(false), 1400);
    }
  }

  return (
    <section
      id="solucion"
      className="relative overflow-hidden px-6 py-28 md:py-36"
    >
      <div className="absolute inset-0 bg-gradient-to-br from-warm-800 via-warm-900 to-foreground" />
      <div className="absolute top-1/4 -right-40 h-[500px] w-[500px] rounded-full bg-gradient-to-br from-primary/20 to-accent/10 blur-[100px] pointer-events-none" />

      <div className="relative z-10 mx-auto max-w-7xl">
        <div ref={sectionRef} className="reveal mb-14 max-w-3xl">
          <span className="mb-5 inline-flex items-center gap-2 rounded-full border border-primary-foreground/10 bg-primary-foreground/10 px-4 py-2 text-xs font-semibold uppercase tracking-wider text-primary-foreground/70">
            {SOLUTION_SECTION.label}
          </span>
          <h2 className="text-balance text-3xl font-bold tracking-tight text-primary-foreground md:text-5xl lg:text-6xl">
            {SOLUTION_SECTION.title}
          </h2>
          <p className="mt-4 max-w-2xl text-base text-primary-foreground/75 md:text-lg">
            {SOLUTION_SECTION.description}
          </p>
        </div>

        <div
          ref={demoRef}
          className="stagger-children grid gap-8 lg:grid-cols-[0.9fr_1.1fr]"
        >
          <div className="space-y-4">
            {SOLUTION_SECTION.steps.map((step, index) => (
              <div
                key={step.number}
                className="min-h-[116px] rounded-2xl border border-primary-foreground/15 bg-primary-foreground/[0.06] p-5"
              >
                <div className="mb-2 flex items-center gap-2.5">
                  <span className="font-mono text-xs text-primary-foreground/50">
                    {step.number}
                  </span>
                  <span
                    className={`h-2 w-2 rounded-full ${
                      activePhase >= index
                        ? "bg-accent progress-pulse"
                        : "bg-primary-foreground/30"
                    }`}
                  />
                  <h3 className="text-sm font-semibold uppercase tracking-wide text-primary-foreground">
                    {step.title}
                  </h3>
                </div>
                <p className="text-sm text-primary-foreground/75">
                  {step.description}
                </p>
              </div>
            ))}
          </div>

          <div className="rounded-3xl border border-primary-foreground/15 bg-warm-900/55 p-1 shadow-2xl shadow-black/20">
            <div className="min-h-[500px] rounded-[22px] bg-gradient-to-b from-warm-900/95 to-warm-800/90 p-5">
              <div className="mb-4 flex items-center justify-between border-b border-primary-foreground/15 pb-3">
                <span className="font-mono text-xs text-primary-foreground/60">
                  dashboard.4gentle.io
                </span>
                <span className="rounded-full bg-primary/20 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-widest text-primary-foreground">
                  Demo en vivo
                </span>
              </div>

              <div className="space-y-4">
                <div className="h-[96px] rounded-2xl border border-primary-foreground/15 bg-primary-foreground/[0.05] p-4">
                  <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-primary-foreground/60">
                    1. Cargar URL
                  </p>
                  <div className="rounded-xl border border-primary-foreground/20 bg-warm-900/70 px-3 py-2 font-mono text-xs text-primary-foreground">
                    {typedUrl}
                    {activePhase === 0 && (
                      <span className="ml-0.5 inline-block h-3.5 w-[1.5px] animate-pulse bg-primary-foreground/70" />
                    )}
                  </div>
                </div>

                <div className="h-[128px] rounded-2xl border border-primary-foreground/15 bg-primary-foreground/[0.05] p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-primary-foreground/60">
                      2. Analisis
                    </p>
                    <p className="font-mono text-xs text-primary-foreground">
                      {progress}%
                    </p>
                  </div>
                  <div className="h-3 w-full overflow-hidden rounded-full bg-warm-700/70">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-primary via-accent to-primary bg-[length:180%_100%] transition-[width] duration-100 ease-linear"
                      style={{
                        width: `${progress}%`,
                        animation: "gradient-text-flow 1.2s linear infinite",
                      }}
                    />
                  </div>
                  <div className="mt-3 flex items-center justify-between text-[11px] text-primary-foreground/60">
                    <span>
                      {activePhase === 1
                        ? "Analizando rutas y tramites web"
                        : "Analisis finalizado y listo para despliegue"}
                    </span>
                    <span className="font-mono">{pages} paginas</span>
                  </div>
                </div>

                <div className="h-[205px] rounded-2xl border border-primary-foreground/15 bg-primary-foreground/[0.05] p-4">
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-primary-foreground/60">
                      3. Script listo
                    </p>
                    <button
                      type="button"
                      onClick={handleCopySnippet}
                      disabled={activePhase !== 2}
                      className={`rounded-lg px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider transition-all ${
                        activePhase !== 2
                          ? "cursor-not-allowed bg-primary-foreground/10 text-primary-foreground/35"
                          : copied
                            ? "bg-emerald-400/20 text-emerald-200"
                            : "bg-primary/25 text-primary-foreground hover:bg-primary/35"
                      } ${copyAnimating ? "scale-95 shadow-inner" : "scale-100"}`}
                    >
                      {copied ? "Copiado" : "Copiar script"}
                    </button>
                  </div>
                  <pre className="h-[150px] overflow-auto rounded-xl bg-warm-900 p-3 font-mono text-[11px] leading-relaxed text-warm-200">
                    <code>
                      {activePhase === 2
                        ? snippet
                        : "// Esperando finalizacion del crawl..."}
                    </code>
                  </pre>
                </div>

                <div className="grid grid-cols-3 gap-2">
                  {phases.map((phaseLabel, idx) => (
                    <div
                      key={phaseLabel}
                      className="rounded-xl border border-primary-foreground/15 bg-primary-foreground/[0.04] px-2 py-2 text-center"
                    >
                      <p className="text-[10px] uppercase tracking-wide text-primary-foreground/55">
                        {phaseLabel}
                      </p>
                      <div
                        className={`mx-auto mt-1 h-1.5 w-8 rounded-full ${
                          activePhase >= idx
                            ? "bg-gradient-to-r from-primary to-accent"
                            : "bg-primary-foreground/20"
                        }`}
                      />
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
