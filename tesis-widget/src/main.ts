import './widget';

type WidgetInitOptions = {
  endpoint?: string;
  apiKey?: string;
  sourceId?: string;
  projectId?: string;
  sessionId?: string;
  theme?: string;
  initialQuestion?: string;
  metadata?: Record<string, unknown>;
};

function applyWidgetConfig(widget: HTMLElement, options?: WidgetInitOptions) {
  if (!options) return;
  if (options.endpoint) widget.setAttribute("endpoint", options.endpoint);
  if (options.apiKey) widget.setAttribute("api-key", options.apiKey);
  if (options.sourceId || options.projectId) widget.setAttribute("source-id", options.sourceId || options.projectId || "");
  if (options.sessionId) widget.setAttribute("session-id", options.sessionId);

  const metadata: Record<string, unknown> = {
    ...(options.metadata || {}),
  };
  if (options.theme) metadata.theme = options.theme;
  if (options.projectId) metadata.project_id = options.projectId;
  if (Object.keys(metadata).length > 0) widget.setAttribute("metadata", JSON.stringify(metadata));
}

function parseScriptInitOptions(): WidgetInitOptions | undefined {
  const script = document.currentScript as HTMLScriptElement | null;
  if (!script) return undefined;
  const ds = script.dataset || {};

  const endpoint = ds.endpoint || script.getAttribute("data-endpoint") || undefined;
  const apiKey = ds.apiKey || script.getAttribute("data-api-key") || undefined;
  const projectId = ds.projectId || script.getAttribute("data-project-id") || undefined;
  const sourceId = ds.sourceId || script.getAttribute("data-source-id") || undefined;
  const sessionId = ds.sessionId || script.getAttribute("data-session-id") || undefined;
  const theme = ds.theme || script.getAttribute("data-theme") || undefined;
  const rawMetadata = ds.metadata || script.getAttribute("data-metadata");
  const initialQuestion = ds.initialQuestion || script.getAttribute("data-initial-question") || undefined;

  let metadata: Record<string, unknown> | undefined;
  if (rawMetadata) {
    try {
      const parsed = JSON.parse(rawMetadata);
      if (parsed && typeof parsed === "object") metadata = parsed as Record<string, unknown>;
    } catch {
      // ignore malformed metadata on script tag
    }
  }

  return {
    endpoint,
    apiKey,
    projectId,
    sourceId,
    sessionId,
    theme,
    initialQuestion,
    metadata,
  };
}

function askWidget(question: string) {
  const q = (question || "").trim();
  if (!q) return;
  const widget = document.querySelector('conversational-widget') as any;
  if (!widget) return;
  widget?.open?.();
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      const root = widget.shadowRoot as ShadowRoot | null;
      const input = root?.querySelector("textarea.input") as HTMLTextAreaElement | null;
      const sendBtn = root?.querySelector("button.send") as HTMLButtonElement | null;
      if (!input || !sendBtn) return;
      input.value = q;
      input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
      sendBtn.click();
    });
  });
}

// Inicializar el widget automáticamente
function initWidget(options?: WidgetInitOptions) {
  // Verificar si ya existe
  const existing = document.querySelector('conversational-widget') as HTMLElement | null;
  if (existing) {
    applyWidgetConfig(existing, options);
    return existing;
  }

  // Crear y añadir el widget al body
  const widget = document.createElement('conversational-widget');
  applyWidgetConfig(widget, options);
  document.body.appendChild(widget);

  return widget;
}

// Capturar config del <script> mientras currentScript sigue disponible.
const bootOptions = parseScriptInitOptions();

// Auto-inicialización cuando el DOM esté listo
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initWidget(bootOptions);
    if (bootOptions?.initialQuestion) askWidget(bootOptions.initialQuestion);
  });
} else {
  initWidget(bootOptions);
  if (bootOptions?.initialQuestion) askWidget(bootOptions.initialQuestion);
}

// Exportar API global
if (typeof window !== 'undefined') {
  (window as any).ConversationalWidget = {
    init: initWidget,
    open: () => {
      const widget = document.querySelector('conversational-widget') as any;
      widget?.open?.();
    },
    close: () => {
      const widget = document.querySelector('conversational-widget') as any;
      widget?.close?.();
    },
    configure: (options: WidgetInitOptions) => {
      const widget = document.querySelector('conversational-widget') as HTMLElement | null;
      if (!widget) return null;
      applyWidgetConfig(widget, options);
      return widget;
    },
    ask: (question: string) => askWidget(question),
  };
}

export { initWidget };
