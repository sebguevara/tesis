"use client";

import { useState } from "react";
import { CrawlForm } from "@/components/dashboard/crawl-form";
import { CrawlStatus } from "@/components/dashboard/crawl-status";
import { WidgetResult } from "@/components/dashboard/widget-result";

export type CrawlPhase = "idle" | "crawling" | "procesando" | "indexing" | "completed" | "failed";

export interface CrawlState {
  phase: CrawlPhase;
  url: string;
  jobId: string;
  progressPct: number;
  pagesCrawled: number;
  etaSeconds: number;
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
  message: "",
  metrics: null,
};

export default function DashboardPage() {
  const [crawlState, setCrawlState] = useState<CrawlState>(initialState);

  function handleStartCrawl(url: string) {
    setCrawlState({
      phase: "crawling",
      url,
      jobId: crypto.randomUUID(),
      progressPct: 0,
      pagesCrawled: 0,
      etaSeconds: 0,
      message: "Iniciando crawl...",
      metrics: null,
    });

    // Simulate crawl phases
    setTimeout(() => {
      setCrawlState((prev) => ({
        ...prev,
        phase: "crawling",
        progressPct: 12,
        pagesCrawled: 23,
        etaSeconds: 890,
        message: "Rastreando paginas (23 encontradas)",
      }));
    }, 1500);

    setTimeout(() => {
      setCrawlState((prev) => ({
        ...prev,
        phase: "procesando",
        progressPct: 44.1,
        pagesCrawled: 87,
        etaSeconds: 538,
        message: "Procesando resultados (87 validas)",
        metrics: {
          total_results: 262,
          accepted_valid_pages: 87,
          saved_docs: 87,
          saved_markdown_files: 87,
          skipped_invalid_content: 4,
          blocked_by_host_filter: 338,
          blocked_by_block_filter: 261,
        },
      }));
    }, 4000);

    setTimeout(() => {
      setCrawlState((prev) => ({
        ...prev,
        phase: "indexing",
        progressPct: 78,
        pagesCrawled: 87,
        etaSeconds: 120,
        message: "Indexando contenido semantico",
      }));
    }, 7000);

    setTimeout(() => {
      setCrawlState((prev) => ({
        ...prev,
        phase: "completed",
        progressPct: 100,
        pagesCrawled: 87,
        etaSeconds: 0,
        message: "Crawl completado. Widget listo.",
        metrics: {
          total_results: 262,
          accepted_valid_pages: 87,
          saved_docs: 87,
          saved_markdown_files: 87,
          skipped_invalid_content: 4,
          blocked_by_host_filter: 338,
          blocked_by_block_filter: 261,
        },
      }));
    }, 10000);
  }

  function handleReset() {
    setCrawlState(initialState);
  }

  if (crawlState.phase === "idle") {
    return <CrawlForm onSubmit={handleStartCrawl} />;
  }

  if (crawlState.phase === "completed") {
    return <WidgetResult crawlState={crawlState} onReset={handleReset} />;
  }

  return <CrawlStatus crawlState={crawlState} />;
}
