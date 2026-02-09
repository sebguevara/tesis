<script lang="ts">
  export let onClose: () => void = () => {};
  export let onRefresh: () => void = () => {};
  export let onExpand: () => void = () => {};

  type Msg = { role: "user" | "assistant"; text: string; ts?: string };

  const today = new Date();
  const dateLabel = today.toLocaleDateString("es-AR", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });

  let input = "";
  let loading = false;

  let messages: Msg[] = [
    { role: "assistant", text: "Hola 👋 ¿En qué puedo ayudarte hoy?", ts: "" },
  ];

  function scrollBottom() {
    requestAnimationFrame(() => {
      const el = document.getElementById("cw-messages");
      if (el) el.scrollTop = el.scrollHeight;
    });
  }

  function send() {
    const t = input.trim();
    if (!t || loading) return;

    messages = [...messages, { role: "user", text: t }];
    input = "";
    loading = true;
    scrollBottom();

    // ⚠️ Reemplazá esto por tu API real
    setTimeout(() => {
      messages = [
        ...messages,
        { role: "assistant", text: "Perfecto ✅ Decime un poco más y lo resolvemos.", ts: "" },
      ];
      loading = false;
      scrollBottom();
    }, 600);
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
          <div class="bubble">…</div>
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
        on:keydown={onKeydown}
        disabled={loading}
      />
      <button class="send" on:click={send} disabled={loading || !input.trim()} aria-label="Enviar">
        ➤
      </button>
    </div>
  </div>
</div>
