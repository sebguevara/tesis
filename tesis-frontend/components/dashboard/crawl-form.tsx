"use client";

import React from "react";
import { useState, useEffect } from "react";
import { DASHBOARD } from "@/lib/constants";

interface CrawlFormProps {
  onSubmit: (url: string) => void;
}

export function CrawlForm({ onSubmit }: CrawlFormProps) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [isFocused, setIsFocused] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");

    try {
      const parsed = new URL(url);
      if (!parsed.protocol.startsWith("http")) {
        setError("La URL debe comenzar con http:// o https://");
        return;
      }
      onSubmit(url);
    } catch {
      setError("Ingresa una URL valida (ej: https://www.tu-institucion.edu)");
    }
  }

  return (
    <div
      className={`flex min-h-[75vh] flex-col items-center justify-center transition-all duration-700 ${
        mounted ? "opacity-100 translate-y-0" : "opacity-0 translate-y-8"
      }`}
    >
      <div className="w-full max-w-xl text-center">
        {/* Animated icon */}
        <div className="mx-auto mb-8 relative">
          <div className="flex h-20 w-20 mx-auto items-center justify-center rounded-3xl bg-gradient-to-br from-primary/20 to-accent/20 backdrop-blur-sm border border-primary/10">
            <svg
              className="h-10 w-10 text-primary"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.5}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 21a9.004 9.004 0 008.716-6.747M12 21a9.004 9.004 0 01-8.716-6.747M12 21c2.485 0 4.5-4.03 4.5-9S14.485 3 12 3m0 18c-2.485 0-4.5-4.03-4.5-9S9.515 3 12 3m0 0a8.997 8.997 0 017.843 4.582M12 3a8.997 8.997 0 00-7.843 4.582m15.686 0A11.953 11.953 0 0112 10.5c-2.998 0-5.74-1.1-7.843-2.918m15.686 0A8.959 8.959 0 0121 12c0 .778-.099 1.533-.284 2.253m0 0A17.919 17.919 0 0112 16.5c-3.162 0-6.133-.815-8.716-2.247m0 0A9.015 9.015 0 013 12c0-1.605.42-3.113 1.157-4.418"
              />
            </svg>
          </div>
          {/* Pulse rings */}
          <div className="absolute inset-0 mx-auto h-20 w-20 rounded-3xl border border-primary/20 animate-ping" style={{ animationDuration: "3s" }} />
        </div>

        <h1 className="mb-3 text-3xl font-bold tracking-tight text-foreground md:text-4xl">
          {DASHBOARD.crawlForm.title}
        </h1>
        <p className="mb-10 text-muted-foreground leading-relaxed">
          {DASHBOARD.crawlForm.subtitle}
        </p>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <div
            className={`relative rounded-2xl transition-all duration-300 ${
              isFocused
                ? "shadow-xl shadow-primary/10 ring-2 ring-primary/30"
                : "shadow-md shadow-warm-300/10"
            }`}
          >
            <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-5">
              <svg
                className={`h-5 w-5 transition-colors ${
                  isFocused ? "text-primary" : "text-muted-foreground/50"
                }`}
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m9.193-9.193a4.5 4.5 0 00-6.364 0l-4.5 4.5a4.5 4.5 0 001.242 7.244"
                />
              </svg>
            </div>
            <input
              type="text"
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
                setError("");
              }}
              onFocus={() => setIsFocused(true)}
              onBlur={() => setIsFocused(false)}
              placeholder={DASHBOARD.crawlForm.placeholder}
              className="w-full rounded-2xl glass-strong py-5 pl-14 pr-5 text-foreground placeholder:text-muted-foreground/40 focus:outline-none transition-all text-base"
            />
          </div>

          {error && (
            <p className="text-sm text-destructive animate-in fade-in-0">{error}</p>
          )}

          <button
            type="submit"
            disabled={!url.trim()}
            className="rounded-2xl bg-gradient-to-r from-primary to-accent py-5 text-base font-semibold text-primary-foreground transition-all hover:shadow-xl hover:shadow-primary/20 hover:scale-[1.01] active:scale-[0.99] disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:scale-100 disabled:hover:shadow-none"
          >
            {DASHBOARD.crawlForm.buttonText}
          </button>
        </form>

        {/* Platform tags */}
        <div className="mt-10 flex flex-wrap items-center justify-center gap-2">
          {["WordPress", "Drupal", "Joomla", "React", "HTML", "Shopify"].map((tag) => (
            <span
              key={tag}
              className="glass rounded-full px-4 py-1.5 text-xs font-medium text-warm-500 transition-all hover:text-primary hover:scale-105"
            >
              {tag}
            </span>
          ))}
          <span className="text-xs text-muted-foreground/50">
            + cualquier plataforma web
          </span>
        </div>
      </div>
    </div>
  );
}
