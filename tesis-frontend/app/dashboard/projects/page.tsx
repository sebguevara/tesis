"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Send } from "lucide-react";
import { toast } from "sonner";
import { WIDGET_SNIPPET } from "@/lib/constants";

interface ProjectItem {
  source_id: string;
  domain: string;
  created_at: string;
  documents_count: number;
  sessions_count: number;
  first_fetched_at: string | null;
  last_fetched_at: string | null;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
}

const CHAT_SESSION_CACHE_KEY = "projects_chat_session_by_source_v1";
const LONG_MESSAGE_THRESHOLD = 560;

function readSessionCache(): Record<string, string> {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(CHAT_SESSION_CACHE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeSessionCache(cache: Record<string, string>) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(CHAT_SESSION_CACHE_KEY, JSON.stringify(cache));
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function splitAssistantMessage(content: string): string[] {
  const clean = (content || "").trim();
  if (!clean) return [""];
  if (clean.length < LONG_MESSAGE_THRESHOLD && clean.split("\n").length <= 10) {
    return [clean];
  }

  const target = Math.floor(clean.length * 0.55);
  const punctuationMatches = Array.from(
    clean.matchAll(/[.!?]\s+|\n\n/g),
    (m) => (typeof m.index === "number" ? m.index + m[0].length : -1),
  ).filter((idx) => idx >= 120 && idx <= clean.length - 120);

  let cut = punctuationMatches.find((idx) => idx >= target) ?? -1;
  if (cut < 0 && punctuationMatches.length > 0) {
    cut = punctuationMatches[punctuationMatches.length - 1];
  }
  if (cut < 0) {
    const newlineCut = clean.lastIndexOf("\n", target);
    cut = newlineCut >= 120 ? newlineCut + 1 : target;
  }
  const first = clean.slice(0, cut).trim();
  const second = clean.slice(cut).trim();
  if (!first || !second) return [clean];
  return [first, second];
}

function formatDate(dateValue: string | null): string {
  if (!dateValue) return "-";
  const date = new Date(dateValue);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString("es-AR", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatChatDayLabel(dateValue: string): string {
  const date = new Date(dateValue);
  if (Number.isNaN(date.getTime())) return "Fecha desconocida";

  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfMessageDay = new Date(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  );
  const diffDays = Math.round(
    (startOfToday.getTime() - startOfMessageDay.getTime()) / 86400000,
  );

  if (diffDays === 0) return "Hoy";
  if (diffDays === 1) return "Ayer";
  return date.toLocaleDateString("es-AR", {
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
}

export default function ProjectsPage() {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [isLoadingProjects, setIsLoadingProjects] = useState(true);
  const [selectedSourceId, setSelectedSourceId] = useState<string>("");
  const [sessionId, setSessionId] = useState<string>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [presenceBetweenChunks, setPresenceBetweenChunks] = useState(false);
  const messagesContainerRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const selectedProject = useMemo(
    () =>
      projects.find((project) => project.source_id === selectedSourceId) ||
      null,
    [projects, selectedSourceId],
  );

  useEffect(() => {
    setSessionId(crypto.randomUUID());
  }, []);

  useEffect(() => {
    async function loadProjects() {
      setIsLoadingProjects(true);
      try {
        const response = await fetch("/api/projects", { cache: "no-store" });
        const payload = (await response.json()) as {
          items?: ProjectItem[];
          detail?: string;
        };
        if (!response.ok)
          throw new Error(payload.detail || "No se pudo cargar proyectos.");
        const items = payload.items || [];
        setProjects(items);
        if (items.length > 0) {
          setSelectedSourceId(items[0].source_id);
        }
      } catch (error) {
        toast.error("No se pudo cargar la vista de proyectos", {
          description:
            error instanceof Error ? error.message : "Error desconocido.",
        });
      } finally {
        setIsLoadingProjects(false);
      }
    }
    void loadProjects();
  }, []);

  useEffect(() => {
    async function loadHistoryForSelectedSource() {
      setMessages([]);
      setInput("");
      if (!selectedSourceId) {
        setSessionId(crypto.randomUUID());
        return;
      }

      const cache = readSessionCache();
      const cachedSessionId = (cache[selectedSourceId] || "").trim();
      const historyUrl = new URL("/api/projects/history", window.location.origin);
      historyUrl.searchParams.set("source_id", selectedSourceId);
      if (cachedSessionId) {
        historyUrl.searchParams.set("session_id", cachedSessionId);
      }

      try {
        const response = await fetch(historyUrl.toString(), { cache: "no-store" });
        const payload = (await response.json()) as {
          session_id?: string | null;
          messages?: Array<{ role?: string; content?: string; created_at?: string }>;
          detail?: string;
        };
        if (!response.ok) throw new Error(payload.detail || "No se pudo cargar historial.");

        const loadedSessionId = (payload.session_id || "").trim();
        if (loadedSessionId) {
          setSessionId(loadedSessionId);
          cache[selectedSourceId] = loadedSessionId;
          writeSessionCache(cache);
        } else {
          setSessionId(cachedSessionId || crypto.randomUUID());
        }

        const loadedMessages = (payload.messages || [])
          .filter((msg) => msg.role === "user" || msg.role === "assistant")
          .map((msg) => ({
            id: crypto.randomUUID(),
            role: msg.role as "user" | "assistant",
            content: msg.content || "",
            createdAt: msg.created_at || new Date().toISOString(),
          }));
        setMessages(loadedMessages);
      } catch {
        setSessionId(cachedSessionId || crypto.randomUUID());
      }
    }
    void loadHistoryForSelectedSource();
  }, [selectedSourceId]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
  }, [messages, isSending]);

  async function handleSendMessage(event: React.FormEvent) {
    event.preventDefault();
    if (!selectedProject) return;
    const question = input.trim();
    if (!question || isSending) return;

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: question,
      createdAt: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMessage]);
    setInput("");
    setIsSending(true);
    setPresenceBetweenChunks(false);

    const appendAssistantResponse = async (answer: string) => {
      const parts = splitAssistantMessage(answer).filter(Boolean);
      if (parts.length <= 1) {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: parts[0] || answer,
            createdAt: new Date().toISOString(),
          },
        ]);
        return;
      }

      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: parts[0],
          createdAt: new Date().toISOString(),
        },
      ]);
      setPresenceBetweenChunks(true);
      await sleep(700);
      setPresenceBetweenChunks(false);
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: parts[1],
          createdAt: new Date().toISOString(),
        },
      ]);
    };

    try {
      const response = await fetch("/api/projects/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          session_id: sessionId || crypto.randomUUID(),
          source_id: selectedProject.source_id,
        }),
      });
      const payload = (await response.json()) as {
        answer?: string;
        detail?: string;
        session_id?: string;
      };
      if (!response.ok)
        throw new Error(payload.detail || "No se pudo obtener respuesta.");

      if (payload.session_id) {
        setSessionId(payload.session_id);
        if (selectedProject?.source_id) {
          const cache = readSessionCache();
          cache[selectedProject.source_id] = payload.session_id;
          writeSessionCache(cache);
        }
      }
      setIsSending(false);
      await appendAssistantResponse(payload.answer || "No se obtuvo respuesta.");
    } catch (error) {
      const detail =
        error instanceof Error && error.message
          ? error.message
          : "No pude responder en este momento. Intenta nuevamente.";
      toast.error("Error en el chat del proyecto", {
        description: detail,
      });
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: detail,
          createdAt: new Date().toISOString(),
        },
      ]);
    } finally {
      setIsSending(false);
      setPresenceBetweenChunks(false);
      inputRef.current?.focus();
    }
  }

  async function handleCopyScript() {
    if (!selectedProject) return;
    try {
      const response = await fetch(
        `/api/widget/snippet?source_id=${encodeURIComponent(selectedProject.source_id)}`,
        { cache: "no-store" },
      );
      const payload = (await response.json()) as {
        source_id?: string;
        api_key?: string;
        widget_query_url?: string;
        detail?: string;
      };
      if (!response.ok || !payload.source_id || !payload.api_key) {
        throw new Error(payload.detail || "No se pudo generar el script.");
      }
      const snippet = WIDGET_SNIPPET({
        sourceId: payload.source_id,
        apiKey: payload.api_key,
      });
      await navigator.clipboard.writeText(snippet);
      toast.success("Script copiado");
    } catch (error) {
      toast.error("No se pudo copiar el script", {
        description:
          error instanceof Error ? error.message : "Error inesperado.",
      });
    }
  }

  return (
    <div className="h-[calc(100vh-5rem)]">
      <div className="grid h-full grid-cols-1 gap-6 lg:grid-cols-[430px_1fr]">
        <section className="glass-strong flex h-full flex-col rounded-3xl border border-border/40 p-4">
          <div className="mb-4">
            <h1 className="text-xl font-semibold text-foreground">Proyectos</h1>
            <p className="text-xs text-muted-foreground">
              Fuentes scrapeadas y guardadas.
            </p>
          </div>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
            {isLoadingProjects ? (
              <div className="glass rounded-2xl p-4 text-sm text-muted-foreground">
                Cargando proyectos...
              </div>
            ) : null}
            {!isLoadingProjects && projects.length === 0 ? (
              <div className="glass rounded-2xl p-4 text-sm text-muted-foreground">
                No hay proyectos cargados aun.
              </div>
            ) : null}

            {projects.map((project) => {
              const isActive = project.source_id === selectedSourceId;
              return (
                <button
                  key={project.source_id}
                  type="button"
                  onClick={() => setSelectedSourceId(project.source_id)}
                  className={`w-full rounded-2xl border p-4 text-left transition-all ${
                    isActive
                      ? "border-primary/40 bg-primary/10 shadow-lg shadow-primary/10"
                      : "border-border/40 bg-card/70 hover:border-primary/20 hover:bg-card"
                  }`}
                >
                  <p className="truncate text-sm font-semibold text-foreground">
                    {project.domain}
                  </p>
                  <p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                    {project.source_id}
                  </p>
                  <div className="mt-3 text-xs">
                    <div className="inline-flex w-fit items-center gap-2 rounded-lg bg-background/60 px-2 py-1 text-left">
                      <span className="text-muted-foreground">Paginas:</span>
                      <span className="font-semibold leading-tight">
                        {project.documents_count}
                      </span>
                    </div>
                    <div className="ml-2 inline-flex w-fit items-center gap-2 rounded-lg bg-background/60 px-2 py-1 text-left">
                      <span className="text-muted-foreground">Sesiones:</span>
                      <span className="font-semibold leading-tight">
                        {project.sessions_count ?? 0}
                      </span>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        <section className="glass-strong flex h-full min-h-0 flex-col rounded-3xl border border-border/40 p-4">
          {selectedProject ? (
            <>
              <div className="mb-4 rounded-2xl border border-border/30 bg-card/70 p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="truncate text-lg font-semibold text-foreground">
                    {selectedProject.domain}
                  </p>
                  <button
                    type="button"
                    onClick={handleCopyScript}
                    className="shrink-0 rounded-lg border border-border/40 bg-background/60 px-3 py-1.5 text-xs font-semibold text-foreground transition-colors hover:border-primary/40"
                  >
                    Copiar script
                  </button>
                </div>
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {selectedProject.source_id}
                </p>
                <div className="mt-3 flex flex-wrap items-start justify-start gap-2 text-xs">
                  <div className="inline-flex w-fit items-center gap-2 rounded-lg bg-background/70 px-2 py-1 text-left">
                    <span className="text-muted-foreground">Paginas:</span>
                    <span className="font-semibold leading-tight">
                      {selectedProject.documents_count}
                    </span>
                  </div>
                  <div className="inline-flex w-fit items-center gap-2 rounded-lg bg-background/70 px-2 py-1 text-left">
                    <span className="text-muted-foreground">Sesiones:</span>
                    <span className="font-semibold leading-tight">
                      {selectedProject.sessions_count ?? 0}
                    </span>
                  </div>
                  <div className="inline-flex w-fit items-center gap-2 rounded-lg bg-background/70 px-2 py-1 text-left text-[11px]">
                    <span className="text-muted-foreground whitespace-nowrap">
                      Primer rastreo:
                    </span>
                    <span className="font-semibold whitespace-nowrap leading-tight">
                      {formatDate(selectedProject.first_fetched_at)}
                    </span>
                  </div>
                  <div className="inline-flex w-fit items-center gap-2 rounded-lg bg-background/70 px-2 py-1 text-left text-[11px]">
                    <span className="text-muted-foreground whitespace-nowrap">
                      Ultimo rastreo:
                    </span>
                    <span className="font-semibold whitespace-nowrap leading-tight">
                      {formatDate(selectedProject.last_fetched_at)}
                    </span>
                  </div>
                </div>
              </div>

              <div
                ref={messagesContainerRef}
                className="min-h-0 flex-1 space-y-3 overflow-y-auto rounded-2xl border border-border/30 bg-background/40 p-4"
              >
                <div className="mb-1 text-xs text-muted-foreground">
                  Sesiones iniciadas:{" "}
                  <span className="font-semibold text-foreground">
                    {selectedProject.sessions_count ?? 0}
                  </span>
                </div>
                {messages.length === 0 ? (
                  <div className="text-sm text-muted-foreground">
                    Haz una pregunta sobre este proyecto.
                  </div>
                ) : null}
                {messages.map((message, index) => (
                  <div key={message.id}>
                    {(index === 0 ||
                      messages[index - 1]?.createdAt?.slice(0, 10) !==
                        message.createdAt.slice(0, 10)) ? (
                      <div className="my-3 flex justify-center">
                        <span className="rounded-full border border-border/30 bg-background/70 px-3 py-1 text-[11px] font-medium text-muted-foreground">
                          {formatChatDayLabel(message.createdAt)}
                        </span>
                      </div>
                    ) : null}
                    <div
                      className={`w-fit max-w-[85%] rounded-2xl px-4 py-3 text-sm ${
                        message.role === "user"
                          ? "ml-auto text-left bg-primary text-primary-foreground"
                          : "mr-auto text-left bg-card border border-border/40 text-foreground"
                      }`}
                    >
                      <p className="whitespace-pre-wrap leading-relaxed">
                        {message.content}
                      </p>
                    </div>
                  </div>
                ))}
                {isSending ? (
                  <div className="w-fit max-w-[85%] rounded-2xl border border-border/40 bg-card px-4 py-3 text-sm text-foreground">
                    <span className="inline-flex items-center gap-1">
                      <span className="h-2 w-2 rounded-full bg-muted-foreground/70 animate-bounce [animation-delay:-0.2s]" />
                      <span className="h-2 w-2 rounded-full bg-muted-foreground/70 animate-bounce [animation-delay:-0.1s]" />
                      <span className="h-2 w-2 rounded-full bg-muted-foreground/70 animate-bounce" />
                    </span>
                  </div>
                ) : null}
                {presenceBetweenChunks ? (
                  <div className="w-fit max-w-[85%] rounded-2xl border border-border/40 bg-card px-4 py-3 text-sm text-foreground">
                    (...)
                  </div>
                ) : null}
              </div>

              <form
                onSubmit={handleSendMessage}
                className="mt-4 flex items-center gap-2"
              >
                <input
                  ref={inputRef}
                  type="text"
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  placeholder="Pregunta sobre este source..."
                  className="w-full rounded-xl border border-border/50 bg-card px-4 py-3 text-sm text-foreground outline-none transition-colors focus:border-primary/40"
                  disabled={isSending}
                />
                <button
                  type="submit"
                  disabled={isSending || !input.trim()}
                  className="inline-flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-r from-primary to-accent text-primary-foreground transition-all hover:scale-[1.02] disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <Send className="h-4 w-4" />
                </button>
              </form>
            </>
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Selecciona un proyecto para ver detalle y abrir chat.
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
