<script lang="ts">
  export let onClose: () => void = () => {};
  export let onRefresh: () => void = () => {};
  export let onExpand: () => void = () => {};
  export let endpoint: string = "/api/widget/query";
  export let apiKey: string = "";
  export let sourceId: string = "";
  export let sessionId: string = "";
  export let metadata: Record<string, unknown> = {};

  type Msg = { role: "user" | "assistant"; text: string; ts?: string };

  const today = new Date();
  const dateLabel = today.toLocaleDateString("es-AR", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });

  let input = "";
  let loading = false;
  let chunkPresence = false;
  let inputEl: HTMLTextAreaElement | null = null;
  let messages: Msg[] = [
    { role: "assistant", text: "Hola, ¿en qué puedo ayudarte hoy?", ts: "" },
  ];
  const LONG_MESSAGE_THRESHOLD = 560;

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function containsNumberedList(text: string): boolean {
    const lines = text.split("\n").map((line) => line.trim());
    const numberedItems = lines.filter((line) => /^\d+\.\s+\S/.test(line));
    return numberedItems.length >= 2;
  }

  function splitAssistantMessage(text: string): string[] {
    const clean = (text || "").trim();
    if (!clean) return [""];
    if (containsNumberedList(clean)) {
      return [clean];
    }
    if (clean.length < LONG_MESSAGE_THRESHOLD && clean.split("\n").length <= 10) {
      return [clean];
    }
    const target = Math.floor(clean.length * 0.55);
    const punctuationMatches = Array.from(
      clean.matchAll(/[.!?]\s+|\n\n/g),
      (m) => (typeof m.index === "number" ? m.index + m[0].length : -1)
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

  async function appendAssistantMessage(text: string) {
    const parts = splitAssistantMessage(text).filter(Boolean);
    if (parts.length <= 1) {
      messages = [...messages, { role: "assistant", text, ts: "" }];
      return;
    }
    messages = [...messages, { role: "assistant", text: parts[0], ts: "" }];
    scrollBottom();
    chunkPresence = true;
    await sleep(700);
    chunkPresence = false;
    messages = [...messages, { role: "assistant", text: parts[1], ts: "" }];
  }

  function scrollBottom() {
    requestAnimationFrame(() => {
      const el = document.getElementById("cw-messages");
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  function focusInput() {
    requestAnimationFrame(() => inputEl?.focus());
  }

  async function send() {
    const t = input.trim();
    if (!t || loading) return;

    if (!apiKey) {
      messages = [...messages, { role: "assistant", text: "Falta `api_key` en la configuración del widget.", ts: "" }];
      scrollBottom();
      return;
    }

    messages = [...messages, { role: "user", text: t }];
    input = "";
    loading = true;
    scrollBottom();
    focusInput();

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": apiKey,
        },
        body: JSON.stringify({
          question: t,
          source_id: sourceId || undefined,
          session_id: sessionId || undefined,
          metadata: {
            ...metadata,
            sent_at: new Date().toISOString(),
          },
        }),
      });

      if (!response.ok) {
        let reason = `HTTP ${response.status}`;
        try {
          const err = await response.json();
          reason = err?.detail || reason;
        } catch {
          // no-op
        }
        throw new Error(reason);
      }

      const data = await response.json();
      sessionId = data?.session_id || sessionId;
      const answer = (data?.answer || "").toString().trim() || "No recibí respuesta del servidor.";
      loading = false;
      await appendAssistantMessage(answer);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Error de red";
      messages = [...messages, { role: "assistant", text: `Error al consultar el backend: ${message}`, ts: "" }];
    } finally {
      loading = false;
      chunkPresence = false;
      scrollBottom();
      focusInput();
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }
</script>

<div class="panel">
  <div class="header">
    <div class="headerDate">{dateLabel}</div>

    <div class="headerRight">
      <button class="iconBtn" on:click={onRefresh} aria-label="Refrescar">⟳</button>
      <button class="iconBtn" on:click={onExpand} aria-label="Expandir">⤢</button>
      <button class="iconBtn" on:click={onClose} aria-label="Cerrar">✕</button>
    </div>
  </div>

  <div class="messages" id="cw-messages">
    <div class="list">
      {#each messages as m}
        <div class={"row " + (m.role === "user" ? "user" : "bot")}>
          <div class="bubble">{m.text}</div>
        </div>
      {/each}

      {#if loading}
        <div class="row bot">
          <div class="bubble thinkingBubble" aria-live="polite" aria-label="Pensando">
            <span class="thinkingLabel">Pensando</span>
            <span class="dots" aria-hidden="true">
              <span class="dot d1"></span><span class="dot d2"></span><span class="dot d3"></span>
            </span>
          </div>
        </div>
      {/if}
      {#if chunkPresence}
        <div class="row bot">
          <div class="bubble presenceBubble" aria-live="polite">(...)</div>
        </div>
      {/if}
    </div>
  </div>

  <div class="inputWrap">
    <div class="inputBar">
      <textarea
        class="input"
        rows="1"
        placeholder="Preguntame algo..."
        bind:value={input}
        bind:this={inputEl}
        on:keydown={onKeydown}
      ></textarea>
      <button class="send" on:click={send} disabled={loading || !input.trim()} aria-label="Enviar">
        ➤
      </button>
    </div>
  </div>
</div>

<style>
  .thinkingBubble {
    min-width: 120px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  .thinkingLabel {
    font-size: 13px;
    color: rgba(71, 85, 105, 0.95);
  }

  .dots {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .dot {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: rgba(245, 158, 11, 0.95);
    opacity: 0.22;
    transform: translateY(0) scale(0.9);
    animation: dotPulse 1s infinite ease-in-out;
  }

  .d1 { animation-delay: 0s; }
  .d2 { animation-delay: 0.2s; }
  .d3 { animation-delay: 0.4s; }

  @keyframes dotPulse {
    0%, 80%, 100% {
      opacity: 0.22;
      transform: translateY(0) scale(0.9);
    }
    40% {
      opacity: 1;
      transform: translateY(-2px) scale(1.1);
    }
  }

  .presenceBubble {
    min-width: 58px;
    text-align: center;
    color: rgba(71, 85, 105, 0.92);
    letter-spacing: 0.04em;
  }
</style>
