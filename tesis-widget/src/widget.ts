import { mount } from "svelte";
import App from "./App.svelte";

/** Shadow DOM base styles (wrapper + collapsed/expanded) */
const shadowStyles = `
  :host {
    all: initial;
    display: block;
    font-family: Arial, Helvetica, sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; }

  /* ✅ Animación continua (sin @property, sin cortes) */
  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Posición widget (SIEMPRE abajo-centro, estable) */
  .widget {
  position: fixed;
  bottom: 2rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 999999;
  width: min(92vw, 520px);

  /* ✅ clave: todo se alinea hacia abajo */
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  }


  /* Estados */
  .hidden { opacity: 0; transform: translateY(10px) scale(0.985); pointer-events: none; }
  .visible { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }

  .fade {
    transition: opacity .22s ease, transform .22s ease;
    transform-origin: bottom center;
    will-change: transform, opacity;
  }

  /* ---------- Collapsed Button ---------- */
  .collapsedBtn {
    position: relative;
    border: 0;
    padding: 0;
    background: transparent;
    cursor: pointer;
    outline: none;
    width: 100%;
  }

  /* ✅ Borde sutil: UNA sola “línea” (streak) que recorre el borde */
  .collapsedBorder {
    position: absolute;
    inset: -1.5px;
    border-radius: 16px;
    overflow: hidden;
    opacity: .85;  /* sutil */
  }

  .collapsedBorder::before {
    content: "";
    position: absolute;
    inset: -60%;          /* grande => nunca “corta” al rotar */
    border-radius: 999px;

    /* ✅ UN SOLO highlight (no dos) */
    background: conic-gradient(
      from 0deg,
      transparent 0 84%,
      rgba(251,191,36,0.00) 84%,
      rgba(251,191,36,0.55) 88%,
      rgba(245,158,11,0.70) 90%,
      rgba(217,119,6,0.65) 92%,
      rgba(146,64,14,0.35) 94%,
      transparent 98% 100%
    );

    animation: spin 18s linear infinite;  /* 🐢 lento */
  }

  /* ✅ Glow MUY leve (tu pedido) */
  .collapsedGlow {
    position: absolute;
    inset: -12px;
    border-radius: 16px;
    filter: blur(22px);
    opacity: 0.035; /* muy bajo */
    background: radial-gradient(
      circle,
      rgba(146,64,14,.18),
      rgba(217,119,6,.10),
      transparent 60%
    );
  }

  /* Input collapsed */
  .collapsedInner {
    position: relative;
    display: flex;
    align-items: center;
    border-radius: 16px;

    /* ✅ input chico */
    padding: 11px 18px;
    min-width: 240px;
    width: 100%;

    background: #fff;
    border: 1px solid rgba(245,158,11,.28);
    box-shadow: 0 8px 18px rgba(0,0,0,.08);
    transition: padding .25s ease, min-width .25s ease, box-shadow .25s ease;
  }

  .collapsedText {
    font-size: 13px;
    color: #a8a29e;
    font-weight: 400;
  }

  /* Hover: expande un poco */
  .collapsedBtn:hover .collapsedInner {
    min-width: 280px;
    padding-left: 22px;
    padding-right: 22px;
    box-shadow: 0 12px 26px rgba(146,64,14,.14);
  }

  /* ---------- Expanded (Modal) ---------- */
  .panelWrap {
    position: relative;
    width: 100%;
  }

  /* ✅ MISMO BORDE que input, pero con radio del modal */
  .panelBorder {
    position: absolute;
    inset: -1.5px;
    border-radius: 24px;
    overflow: hidden;
    opacity: .85;
  }

  .panelBorder::before {
    content: "";
    position: absolute;
    inset: -60%;
    border-radius: 999px;
    background: conic-gradient(
      from 0deg,
      transparent 0 84%,
      rgba(251,191,36,0.00) 84%,
      rgba(251,191,36,0.55) 88%,
      rgba(245,158,11,0.70) 90%,
      rgba(217,119,6,0.65) 92%,
      rgba(146,64,14,0.35) 94%,
      transparent 98% 100%
    );
    animation: spin 22s linear infinite; /* 🐢 más lento en modal */
  }

  /* Glow modal muy leve */
  .panelGlow {
    position: absolute;
    inset: -26px;
    border-radius: 24px;
    filter: blur(30px);
    opacity: 0.035;
    background: radial-gradient(
      circle,
      rgba(146,64,14,.22),
      rgba(217,119,6,.10),
      transparent 60%
    );
  }

  .collapsed,
  .expanded {
    width: 100%;
  }

  .expanded {
  position: absolute;
  left: 0;
  bottom: 0;     /* ✅ siempre abajo */
}

.collapsed {
  position: absolute;
  left: 0;
  bottom: 0;     /* ✅ siempre abajo */
}

  #svelte-root { position: relative; }
`;

/** Svelte UI styles: chat limpio tipo ChatGPT + header con fecha y 3 iconos */
const svelteStyles = `
  .panel {
    position: relative;
    display: flex;
    flex-direction: column;
    width: 100%;
    height: min(640px, 72vh);
    border-radius: 24px;
    overflow: hidden;

    background: #ffffff;
    border: 1px solid rgba(245,158,11,.20);
    box-shadow: 0 28px 70px rgba(70, 40, 20, 0.16);
  }

  /* Header: SOLO fecha + iconos */
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 14px;
    background: linear-gradient(135deg, #fff7ed, #fffbeb);
    border-bottom: 1px solid rgba(245,158,11,.22);
  }

  .headerDate {
    font-size: 12px;
    color: rgba(120,113,108,1);
    letter-spacing: .2px;
    font-weight: 600;
  }

  .headerRight { display:flex; align-items:center; gap: 8px; }

  .iconBtn {
    width: 34px;
    height: 34px;
    border-radius: 12px;
    border: 1px solid rgba(245,158,11,.20);
    background: rgba(255,255,255,.75);
    cursor: pointer;
    color: rgba(120,113,108,1);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: transform .15s ease, background .15s ease, border-color .15s ease;
    user-select: none;
  }
  .iconBtn:hover {
    transform: translateY(-1px);
    background: rgba(255,255,255,.95);
    border-color: rgba(245,158,11,.35);
  }

  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px 14px 12px;
    background: linear-gradient(to bottom, rgba(255,247,237,.35), transparent 45%);
  }

  .list { display: flex; flex-direction: column; gap: 12px; }

  .row { display: flex; }
  .row.user { justify-content: flex-end; }
  .row.bot { justify-content: flex-start; }

  .bubble {
    max-width: 78%;
    padding: 12px 14px;
    border-radius: 18px;
    font-size: 14px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* user bubble */
  .row.user .bubble {
    border-bottom-right-radius: 8px;
    color: #fff;
    background: linear-gradient(135deg, #f59e0b, #d97706);
    box-shadow: 0 10px 20px rgba(245,158,11,.16);
  }

  /* assistant bubble */
  .row.bot .bubble {
    border-bottom-left-radius: 8px;
    background: rgba(255,255,255,.88);
    border: 1px solid rgba(245,158,11,.22);
    color: rgba(15,23,42,1);
    box-shadow: 0 8px 16px rgba(146,64,14,.08);
  }

  .inputWrap {
    padding: 12px 12px 14px;
    border-top: 1px solid rgba(245,158,11,.18);
    background: linear-gradient(to top, rgba(255,247,237,.75), transparent);
  }

  /* input bar suave */
  .inputBar {
    display: flex;
    align-items: center;
    border-radius: 999px;
    border: 1px solid rgba(245,158,11,.26);
    background: rgba(255,255,255,.92);
    box-shadow: 0 10px 22px rgba(146,64,14,0.07);
  }

  .input {
    flex: 1;
    border: 0;
    outline: none;
    background: transparent;
    padding: 12px 14px;
    font-size: 14px;
    color: rgba(15,23,42,1);
    resize: none;
    line-height: 1.35;
    max-height: 110px;
  }
  .input::placeholder { color: rgba(120,113,108,1); }

  .send {
    margin-right: 8px;
    width: 36px;
    height: 36px;
    border-radius: 999px;
    border: 0;
    cursor: pointer;
    color: white;
    background: linear-gradient(135deg, #f59e0b, #d97706);
    box-shadow: 0 10px 22px rgba(245,158,11,.14);
    transition: transform .15s ease, filter .15s ease;
  }
  .send:hover { transform: translateY(-1px); filter: brightness(1.03); }
  .send:disabled { opacity: .45; cursor: not-allowed; }

  /* scrollbar */
  .messages::-webkit-scrollbar { width: 10px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb {
    background: rgba(245,158,11,.22);
    border-radius: 999px;
    border: 3px solid transparent;
    background-clip: padding-box;
  }
  .messages::-webkit-scrollbar-thumb:hover { background: rgba(245,158,11,.34); }
`;

export class ConversationalWidget extends HTMLElement {
  private shadow: ShadowRoot;
  private container: HTMLDivElement | null = null;
  private svelteApp: any = null;
  private isOpen = false;

  constructor() {
    super();
    this.shadow = this.attachShadow({ mode: "open" });
  }

  connectedCallback() {
    this.render();
  }

  disconnectedCallback() {
    this.svelteApp?.$destroy?.();
  }

  private render() {
    this.shadow.innerHTML = "";

    const baseStyle = document.createElement("style");
    baseStyle.textContent = shadowStyles;
    this.shadow.appendChild(baseStyle);

    const uiStyle = document.createElement("style");
    uiStyle.textContent = svelteStyles;
    this.shadow.appendChild(uiStyle);

    this.container = document.createElement("div");
    this.container.className = "widget";

    // Collapsed
    const collapsed = document.createElement("div");
    collapsed.className = "collapsed fade visible";
    collapsed.id = "collapsed";

    const btn = document.createElement("button");
    btn.className = "collapsedBtn";
    btn.setAttribute("aria-label", "Abrir chat");
    btn.addEventListener("click", () => this.open());

    const border = document.createElement("div");
    border.className = "collapsedBorder";

    const glow = document.createElement("div");
    glow.className = "collapsedGlow";

    const inner = document.createElement("div");
    inner.className = "collapsedInner";

    const text = document.createElement("div");
    text.className = "collapsedText";
    text.textContent = "Preguntame algo...";

    inner.appendChild(text);

    btn.appendChild(border);
    btn.appendChild(glow);
    btn.appendChild(inner);
    collapsed.appendChild(btn);

    // Expanded
    const expanded = document.createElement("div");
    expanded.className = "expanded fade hidden";
    expanded.id = "expanded";

    const panelWrap = document.createElement("div");
    panelWrap.className = "panelWrap";

    const panelBorder = document.createElement("div");
    panelBorder.className = "panelBorder";

    const panelGlow = document.createElement("div");
    panelGlow.className = "panelGlow";

    const svelteRoot = document.createElement("div");
    svelteRoot.id = "svelte-root";

    panelWrap.appendChild(panelBorder);
    panelWrap.appendChild(panelGlow);
    panelWrap.appendChild(svelteRoot);
    expanded.appendChild(panelWrap);

    this.container.appendChild(collapsed);
    this.container.appendChild(expanded);
    this.shadow.appendChild(this.container);
  }

  public open() {
    const collapsed = this.shadow.getElementById("collapsed")!;
    const expanded = this.shadow.getElementById("expanded")!;
    const svelteRoot = this.shadow.querySelector("#svelte-root") as HTMLElement;

    this.isOpen = true;

    collapsed.classList.remove("visible");
    collapsed.classList.add("hidden");

    expanded.classList.remove("hidden");
    expanded.classList.add("visible");

    if (!this.svelteApp) {
      this.svelteApp = mount(App, {
        target: svelteRoot,
        props: {
          onClose: () => this.close(),
          onRefresh: () =>
            this.dispatchEvent(new CustomEvent("widget:refresh")),
          onExpand: () => this.dispatchEvent(new CustomEvent("widget:expand")),
        },
      });
    }
  }

  public close() {
    const collapsed = this.shadow.getElementById("collapsed")!;
    const expanded = this.shadow.getElementById("expanded")!;

    this.isOpen = false;

    expanded.classList.remove("visible");
    expanded.classList.add("hidden");

    collapsed.classList.remove("hidden");
    collapsed.classList.add("visible");
  }
}

if (
  typeof window !== "undefined" &&
  !customElements.get("conversational-widget")
) {
  customElements.define("conversational-widget", ConversationalWidget);
}
