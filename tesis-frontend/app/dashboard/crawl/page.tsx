"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { CrawlForm } from "@/components/dashboard/crawl-form";
import { CrawlStatus } from "@/components/dashboard/crawl-status";
import { WidgetResult } from "@/components/dashboard/widget-result";

export type CrawlPhase =
  | "idle"
  | "starting"
  | "crawling"
  | "procesando"
  | "indexing"
  | "completed"
  | "failed";

export interface CrawlState {
  phase: CrawlPhase;
  url: string;
  jobId: string;
  progressPct: number;
  pagesCrawled: number;
  etaSeconds: number;
  startedAtMs: number | null;
  finishedAtMs: number | null;
  totalDurationSeconds: number | null;
  message: string;
  metrics: {
    total_results: number;
    accepted_valid_pages: number;
    saved_docs: number;
    saved_markdown_files: number;
    skipped_invalid_content: number;
    blocked_by_host_filter: number;
    blocked_by_block_filter: number;
  } | null;
}

const initialState: CrawlState = {
  phase: "idle",
  url: "",
  jobId: "",
  progressPct: 0,
  pagesCrawled: 0,
  etaSeconds: 0,
  startedAtMs: null,
  finishedAtMs: null,
  totalDurationSeconds: null,
  message: "",
  metrics: null,
};

interface BackendJobStatus {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  phase: string;
  message: string;
  progress_pct: number;
  eta_seconds: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  pages_crawled: number;
  metrics: CrawlState["metrics"] & {
    successful_results?: number;
  };
}

const DEFAULT_SCRAPE_CONFIG = {
  max_pages: 5000,
  concurrency: 10,
  max_depth: 5,
  persist_to_db: true,
  save_markdown_files: false,
  use_allow_filter: true,
  min_content_words: 5,
  count_valid_pages_only: true,
  block_old_years: true,
} as const;

const ACTIVE_CRAWL_STORAGE_KEY = "active_crawl_job_v1";

interface ActiveCrawlCache {
  jobId: string;
  url: string;
  startedAtMs: number;
}

function readActiveCrawlCache(): ActiveCrawlCache | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(ACTIVE_CRAWL_STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as ActiveCrawlCache;
    if (!parsed?.jobId || !parsed?.url || !parsed?.startedAtMs) return null;
    return parsed;
  } catch {
    return null;
  }
}

function saveActiveCrawlCache(data: ActiveCrawlCache) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(ACTIVE_CRAWL_STORAGE_KEY, JSON.stringify(data));
}

function clearActiveCrawlCache() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(ACTIVE_CRAWL_STORAGE_KEY);
}

function parseIsoToMs(value?: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function mapDisplayProgress(job: BackendJobStatus): number {
  const raw = Number(job.progress_pct || 0);
  if (job.status === "completed") {
    return Math.max(0, Math.min(100, raw));
  }
  return Math.max(0, Math.min(99, raw));
}

export default function DashboardPage() {
  const [crawlState, setCrawlState] = useState<CrawlState>(initialState);
  const [notificationPermission, setNotificationPermission] = useState<
    NotificationPermission | "unsupported"
  >("unsupported");
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const notifiedCompletedJobsRef = useRef<Set<string>>(new Set());
  const notifiedFailedJobsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (typeof window !== "undefined" && "Notification" in window) {
      setNotificationPermission(Notification.permission);
    }
  }, []);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
      }
    };
  }, []);

  async function requestNotificationPermission() {
    if (typeof window === "undefined" || !("Notification" in window)) {
      setNotificationPermission("unsupported");
      return;
    }
    const permission = await Notification.requestPermission();
    setNotificationPermission(permission);
  }

  function buildInteractiveMessage(job: BackendJobStatus): string {
    const phase = (job.phase || "").toLowerCase();
    const metrics = job.metrics || null;
    const relevantPages =
      metrics?.accepted_valid_pages ?? job.pages_crawled ?? 0;
    const savedDocs = metrics?.saved_docs ?? 0;

    if (job.status === "completed") {
      return `Completado con exito. ${relevantPages} paginas relevantes encontradas y ${savedDocs} documentos guardados.`;
    }
    if (job.status === "failed") {
      return "El proceso fallo. Revisa la URL e intenta nuevamente.";
    }
    if (phase.includes("inici")) {
      return "Preparando el crawler y configurando el analisis del sitio.";
    }
    if (phase.includes("rastre")) {
      return `Rastreando el sitio para encontrar contenido.`;
    }
    if (phase.includes("proces")) {
      return job.message || `Analizando y guardando contenido.`;
    }
    if (phase.includes("index")) {
      return `Indexando y finalizando el analisis (${relevantPages} paginas relevantes).`;
    }
    return job.message || "Procesando el scraping...";
  }

  function mapBackendPhase(job: BackendJobStatus): CrawlPhase {
    if (job.status === "completed") return "completed";
    if (job.status === "failed") return "failed";
    const phase = (job.phase || "").toLowerCase();
    if (phase.includes("proces")) return "procesando";
    if (phase.includes("index")) return "indexing";
    return "crawling";
  }

  function notifyCompleted(job: BackendJobStatus) {
    const relevantPages =
      job.metrics?.accepted_valid_pages ?? job.pages_crawled ?? 0;
    toast.success("Crawl completado con exito", {
      description: `${relevantPages} paginas relevantes encontradas y guardadas.`,
      duration: 7000,
    });

    if (
      typeof window !== "undefined" &&
      "Notification" in window &&
      Notification.permission === "granted"
    ) {
      new Notification("4gentle: proceso completado", {
        body: `Se completó el analisis con ${relevantPages} paginas relevantes.`,
      });
    }
  }

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const pollJobStatus = useCallback(
    async (jobId: string, url: string): Promise<BackendJobStatus | null> => {
      try {
        const response = await fetch(`/api/crawl/status/${jobId}`, {
          method: "GET",
          cache: "no-store",
        });
        const payload = (await response.json()) as
          | BackendJobStatus
          | { detail?: string };

        if (!response.ok) {
          throw new Error(
            (payload as { detail?: string }).detail ||
              "No se pudo consultar el estado.",
          );
        }

        const job = payload as BackendJobStatus;
        const mappedPhase = mapBackendPhase(job);
        const message = buildInteractiveMessage(job);

        setCrawlState((prev) => {
          const startedAtMs =
            parseIsoToMs(job.started_at) ?? prev.startedAtMs ?? Date.now();
          const finishedAtMs =
            job.status === "completed"
              ? (parseIsoToMs(job.finished_at) ?? Date.now())
              : null;
          const totalDurationSeconds =
            finishedAtMs != null
              ? Math.max(0, Math.round((finishedAtMs - startedAtMs) / 1000))
              : null;

          return {
            phase: mappedPhase,
            url,
            jobId: job.job_id,
            progressPct: mapDisplayProgress(job),
            pagesCrawled: Number(
              job.pages_crawled ?? job.metrics?.accepted_valid_pages ?? 0,
            ),
            etaSeconds: Number(job.eta_seconds || 0),
            startedAtMs,
            finishedAtMs,
            totalDurationSeconds,
            message,
            metrics: job.metrics || null,
          };
        });

        if (job.status === "completed") {
          stopPolling();
          clearActiveCrawlCache();
          if (!notifiedCompletedJobsRef.current.has(job.job_id)) {
            notifiedCompletedJobsRef.current.add(job.job_id);
            notifyCompleted(job);
          }
        } else if (job.status === "failed") {
          stopPolling();
          clearActiveCrawlCache();
          if (!notifiedFailedJobsRef.current.has(job.job_id)) {
            notifiedFailedJobsRef.current.add(job.job_id);
            toast.error("El crawl no pudo completarse", {
              description: message,
              duration: 7000,
            });
          }
        }
        return job;
      } catch (error) {
        stopPolling();
        const detail =
          error instanceof Error
            ? error.message
            : "No se pudo consultar el estado del crawl.";
        setCrawlState((prev) => ({
          ...prev,
          phase: "failed",
          message: detail,
        }));
        toast.error("Error consultando el estado", {
          description: detail,
        });
        return null;
      }
    },
    [stopPolling],
  );

  useEffect(() => {
    const activeCrawl = readActiveCrawlCache();
    if (!activeCrawl) return;

    setCrawlState({
      phase: "crawling",
      url: activeCrawl.url,
      jobId: activeCrawl.jobId,
      progressPct: 1,
      pagesCrawled: 0,
      etaSeconds: 0,
      startedAtMs: activeCrawl.startedAtMs,
      finishedAtMs: null,
      totalDurationSeconds: null,
      message: "Reconectando con el proceso de crawl en curso...",
      metrics: null,
    });

    void (async () => {
      const job = await pollJobStatus(activeCrawl.jobId, activeCrawl.url);
      if (!job) return;
      if (job.status === "running" || job.status === "pending") {
        pollTimerRef.current = setInterval(() => {
          void pollJobStatus(activeCrawl.jobId, activeCrawl.url);
        }, 5000);
      }
    })();
  }, [pollJobStatus]);

  async function handleStartCrawl(url: string) {
    if (notificationPermission === "default") {
      void requestNotificationPermission();
    }

    stopPolling();

    const startedAtMs = Date.now();
    setCrawlState({
      phase: "starting",
      url,
      jobId: "",
      progressPct: 2,
      pagesCrawled: 0,
      etaSeconds: 0,
      startedAtMs,
      finishedAtMs: null,
      totalDurationSeconds: null,
      message: "Iniciando el crawl. Esto puede tardar varios minutos.",
      metrics: null,
    });

    try {
      const response = await fetch("/api/crawl/start", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          url,
          ...DEFAULT_SCRAPE_CONFIG,
        }),
      });
      const payload = (await response.json()) as {
        job_id?: string;
        detail?: string;
      };
      if (!response.ok || !payload.job_id) {
        throw new Error(
          payload.detail || "No se pudo iniciar el proceso de scraping.",
        );
      }

      const jobId = payload.job_id;
      saveActiveCrawlCache({ jobId, url, startedAtMs });
      setCrawlState((prev) => ({
        ...prev,
        phase: "crawling",
        jobId,
        message:
          "Crawl iniciado. Estamos rastreando y analizando contenido relevante.",
      }));
      toast.info("Crawl iniciado", {
        description:
          "Rastrearemos hasta 5000 paginas relevantes. Este proceso puede tardar varios minutos.",
      });

      const job = await pollJobStatus(jobId, url);
      if (job && (job.status === "running" || job.status === "pending")) {
        pollTimerRef.current = setInterval(() => {
          void pollJobStatus(jobId, url);
        }, 5000);
      }
    } catch (error) {
      const detail =
        error instanceof Error
          ? error.message
          : "No se pudo iniciar el proceso de scraping.";
      setCrawlState((prev) => ({
        ...prev,
        phase: "failed",
        message: detail,
      }));
      toast.error("Error al iniciar el crawl", {
        description: detail,
      });
    }
  }

  function handleReset() {
    stopPolling();
    clearActiveCrawlCache();
    setCrawlState(initialState);
  }

  if (crawlState.phase === "idle" || crawlState.phase === "starting") {
    return (
      <CrawlForm
        onSubmit={handleStartCrawl}
        notificationPermission={notificationPermission}
        onRequestNotifications={requestNotificationPermission}
        isStarting={crawlState.phase === "starting"}
        startMessage={crawlState.message}
      />
    );
  }

  if (crawlState.phase === "completed") {
    return <WidgetResult crawlState={crawlState} onReset={handleReset} />;
  }

  return (
    <CrawlStatus
      crawlState={crawlState}
      notificationPermission={notificationPermission}
      onRequestNotifications={requestNotificationPermission}
    />
  );
}
