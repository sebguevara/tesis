// ============================================================
// 4gentle - Constants & Configuration
// All texts, colors, images, and variables live here
// ============================================================

// --- Brand ---
export const BRAND = {
  name: "4gentle",
  tagline: "Navegacion conversacional para plataformas institucionales",
  logo: "/logo.svg",
} as const;

// --- Landing Page Copy ---
export const HERO = {
  preHeadline: "Para equipos directivos de universidades",
  headline: "Convierte tu sitio web en una conversacion",
  subHeadline:
    "4gentle transforma busquedas confusas en respuestas directas. Integracion simple, sin redisenar tu portal.",
} as const;

export const PROBLEM_SECTION = {
  label: "El problema",
  title: "La informacion existe. La ruta no siempre es clara.",
  description:
    "En portales universitarios, encontrar el tramite correcto todavia consume tiempo y soporte.",
  painPoints: [
    {
      title: "Navegacion extensa",
      description:
        "Demasiados niveles para una consulta simple.",
    },
    {
      title: "Buscador limitado",
      description:
        "Coincidencias por palabra, no por intencion.",
    },
    {
      title: "Soporte saturado",
      description:
        "Consultas repetidas en canales institucionales.",
    },
  ],
} as const;

export const SOLUTION_SECTION = {
  label: "La solucion",
  title: "Una URL, analisis automatico y widget listo.",
  description:
    "Flujo completo en minutos: descubrimiento, indexacion y despliegue.",
  steps: [
    {
      number: "01",
      title: "Pegas la URL",
      description:
        "Inicias el rastreo desde el dashboard.",
    },
    {
      number: "02",
      title: "4gentle rastrea",
      description:
        "Procesa contenidos y estructura institucional.",
    },
    {
      number: "03",
      title: "Copias el script",
      description:
        "Publicas el widget en tu portal.",
    },
  ],
} as const;

export const FEATURES_SECTION = {
  label: "Capacidades",
  title: "Resultados concretos para gestion universitaria",
  features: [
    {
      title: "Cobertura completa del portal",
      description:
        "Indexa contenido util y evita ruido tecnico.",
      metric: "5,000+",
      metricLabel: "paginas por dominio",
    },
    {
      title: "Respuestas por intencion",
      description:
        "Guia a cada usuario al tramite correcto.",
      metric: "< 2s",
      metricLabel: "respuesta promedio",
    },
    {
      title: "Implementacion rapida",
      description:
        "Un snippet. Sin cambios de CMS ni migraciones.",
      metric: "1 linea",
      metricLabel: "para integrar",
    },
    {
      title: "Metricas de uso",
      description:
        "Visibilidad de consultas y fricciones frecuentes.",
      metric: "100%",
      metricLabel: "queries trazables",
    },
  ],
} as const;

export const TECH_SECTION = {
  label: "Motor de descubrimiento",
  title: "Como el crawler convierte miles de paginas en respuestas precisas",
  specs: [
    { label: "Crawl multinivel", value: "Explora menus, enlaces profundos y documentos institucionales sin romper performance." },
    { label: "Parsing inteligente", value: "Extrae estructura academica, fechas clave, requisitos y pasos accionables por tramite." },
    { label: "Index semantico", value: "Construye embeddings por contexto para resolver intenciones, no solo palabras sueltas." },
    { label: "Orquestacion RAG", value: "Recupera evidencia del portal y genera respuestas trazables con contexto actualizado." },
    { label: "Widget liviano", value: "Snippet embebible, carga asincronica y despliegue inmediato en cualquier CMS." },
    { label: "Operacion estable", value: "Rate limiting, control de errores y telemetria para entornos universitarios de alto trafico." },
  ],
} as const;

export const CLOSING_SECTION = {
  preHeadline: "Menos friccion, mas resolucion",
  headline: "Informacion institucional accesible al instante.",
  subHeadline:
    "4gentle convierte tu portal universitario en una experiencia guiada, medible y simple de usar.",
} as const;

export const FOOTER = {
  title: "Equipo del proyecto",
  students: ["Bricia Díaz", "Sebastian Guevara"],
  academicNote:
    "Trabajo final de la Licenciatura en Sistemas de Informacion (LSI) - Universidad Nacional del Nordeste (UNNE).",
} as const;

// --- Dashboard ---
export const DASHBOARD = {
  nav: {
    brand: "4gentle",
    links: [
      { label: "Proyectos", href: "/dashboard" },
      { label: "Documentacion", href: "#" },
    ],
  },
  crawlForm: {
    title: "Nuevo proyecto",
    subtitle: "Ingresa la URL de tu plataforma institucional y 4gentle se encargara del resto.",
    placeholder: "https://www.tu-institucion.edu",
    buttonText: "Iniciar crawl",
  },
  crawlStatus: {
    title: "Crawl en progreso",
    message:
      "Estamos recorriendo y analizando tu plataforma. Este proceso puede tomar varios minutos dependiendo del tamano del sitio. Podes hacer otra cosa mientras tanto, te notificaremos cuando este listo.",
    phases: {
      crawling: "Rastreando paginas",
      procesando: "Procesando resultados",
      indexing: "Indexando contenido",
      completed: "Completado",
      failed: "Error en el proceso",
    } as Record<string, string>,
  },
  widgetReady: {
    title: "Tu widget esta listo",
    subtitle: "Copia el siguiente snippet y pegalo en tu sitio antes del cierre del tag </body>.",
  },
  integrationGuides: [
    {
      platform: "WordPress",
      icon: "wordpress",
      steps: [
        "Accede al panel de administracion de WordPress.",
        "Ve a Apariencia > Editor de temas (o usa un plugin como Insert Headers and Footers).",
        "Pega el snippet justo antes de </body> en footer.php.",
        "Guarda los cambios y verifica en el frontend.",
      ],
    },
    {
      platform: "HTML estatico",
      icon: "code",
      steps: [
        "Abre tu archivo index.html (o el layout principal).",
        "Localiza el cierre del tag </body>.",
        "Pega el snippet justo antes de esa linea.",
        "Subi los cambios a tu servidor.",
      ],
    },
    {
      platform: "React / Next.js",
      icon: "react",
      steps: [
        "Abre tu componente raiz (layout.tsx o _app.tsx).",
        "Usa el componente <Script> de Next.js o un useEffect para inyectar el script.",
        "Agrega strategy='afterInteractive' para no bloquear el render.",
        "Despliega tu aplicacion normalmente.",
      ],
    },
    {
      platform: "Joomla",
      icon: "globe",
      steps: [
        "Ve a Extensiones > Plantillas > Tu plantilla activa.",
        "Edita el archivo index.php de la plantilla.",
        "Pega el snippet antes del cierre </body>.",
        "Guarda y limpia la cache de Joomla.",
      ],
    },
    {
      platform: "Drupal",
      icon: "layers",
      steps: [
        "Ve a Administracion > Apariencia > Tu tema activo > Configuracion.",
        "Usa el modulo Asset Injector o edita html.html.twig.",
        "Pega el snippet en la region page_bottom.",
        "Limpia la cache de Drupal.",
      ],
    },
    {
      platform: "Shopify",
      icon: "shopping-bag",
      steps: [
        "Ve a Tienda online > Temas > Acciones > Editar codigo.",
        "Abre el archivo theme.liquid.",
        "Pega el snippet antes de </body>.",
        "Guarda los cambios.",
      ],
    },
  ],
} as const;

// --- Mock Scraping Status (from real API response structure) ---
export const MOCK_SCRAPING_STATUS = {
  job_id: "a5ed3961-8d21-44eb-b3ff-a044205482bc",
  status: "running" as const,
  phase: "procesando",
  message: "Procesando resultados (87 validas)",
  progress_pct: 44.1,
  eta_seconds: 538,
  pages_crawled: 87,
  errors: [] as string[],
  started_at: "2026-02-09T09:28:29.868786",
  finished_at: null,
  last_updated_at: "2026-02-09T09:35:34.593477",
  metrics: {
    total_results: 262,
    finished_reason: "running",
    target_valid_pages: 5000,
    crawl_budget_pages: 20000,
    accepted_valid_pages: 87,
    successful_results: 91,
    saved_docs: 87,
    saved_markdown_files: 87,
    skipped_invalid_content: 4,
    skipped_ingestion: 0,
    skipped_db_disabled: 0,
    blocked_by_host_filter: 338,
    blocked_by_allow_filter: 0,
    matched_allow_filter: 2,
    blocked_by_block_filter: 261,
  },
} as const;

// --- Widget Snippet Template ---
export const WIDGET_SNIPPET = (projectId: string) =>
  `<!-- 4gentle Widget -->
<script
  src="https://cdn.4gentle.io/widget.js"
  data-project-id="${projectId}"
  data-theme="warm"
  async
><\/script>`;
