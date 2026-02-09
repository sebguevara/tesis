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
  let inputEl: HTMLTextAreaElement | null = null;
  let messages: Msg[] = [
    { role: "assistant", text: "Hola 👋 ¿En qué puedo ayudarte hoy?", ts: "" },
  ];

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
      messages = [...messages, { role: "assistant", text: answer, ts: "" }];
    } catch (err) {
      const message = err instanceof Error ? err.message : "Error de red";
      messages = [...messages, { role: "assistant", text: `Error al consultar el backend: ${message}`, ts: "" }];
    } finally {
      loading = false;
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
</style>
