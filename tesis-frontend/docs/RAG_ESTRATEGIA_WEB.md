# Estrategia RAG Para Sitios Web (UNNE / Facultad)

## Objetivo
Construir un RAG que responda **rápido, preciso y verificable** para perfiles como `Ingresantes`, `Estudiantes`, `Docentes`, `NoDocentes`, `Directivos`.

Metas:
- Respuestas con evidencia concreta (URL + fragmento útil).
- Cero inventos.
- Manejo de conflicto entre fuentes (priorizar canónicas y recientes).
- Sin re-scraping en cada pregunta.

---

## Problemas Detectados En El Sistema Actual
1. Se mezcló extracción regex agresiva (`program_facts`) con páginas no canónicas.
2. Entró ruido (ej: "104 años" desde sitemap), contaminando respuestas.
3. El query path intentó hacer live scraping durante preguntas (latencia enorme).
4. El chunking y limpieza removieron estructura útil (ej: `Materia:` repetido).
5. Faltó una política fuerte de ranking por autoridad + recencia + tipo de página.

---

## Estrategia Recomendada (Arquitectura)

### 1) Indexación En Capas
Guardar por URL:
- `raw_html`
- `clean_text` (boilerplate removido)
- `metadata`: `{title, canonical_url, fetched_at, page_type, language}`
- `chunks`
- `embedding`

Nunca depender solo de facts regex para responder.

### 2) Clasificación De Página (clave)
Asignar `page_type` fuerte:
- `career_canonical` (`/carreras/...`)
- `offer_canonical` (`/oferta-academica/...`, `/ofertas-acad/...`)
- `procedure` (`/tramites`, `/admis`, `/ingres`)
- `news_event` (noticias, agenda, prensa)
- `utility_noise` (mapa-de-sitio, tags, archives)

Regla: `utility_noise` y `news_event` no deben ganar respuestas factuales de carrera.

### 3) Recuperación Híbrida
Usar:
- BM25/lexical
- Dense vector retrieval
- Fusión con RRF
- Reranker (cross-encoder) para top-k final

Esto mejora recall + precisión en web real.

### 4) Respuesta "Evidence-First"
Pipeline de respuesta:
1. Recuperar top candidates.
2. Reordenar por score compuesto.
3. Extraer evidencia literal mínima.
4. Responder **solo** si hay soporte explícito.
5. Si hay conflicto: explicar y elegir fuente más confiable.

---

## Política De Ranking (score compuesto)
Definir un `final_score` por documento/chunk:

`final_score = retrieval_score + authority_bonus + freshness_bonus - noise_penalty + intent_match_bonus`

### Authority bonus
- `+40` si URL contiene `/carreras/`
- `+20` si URL contiene `/oferta-academica/` o `/ofertas-acad/`
- `-30` si URL es sitemap/tag/archive
- `-20` si URL es noticia/evento para preguntas de autoridad/duración/plan

### Freshness bonus
- `+15` <= 30 días
- `+10` <= 90 días
- `+5` <= 180 días
- `-8` > 365 días

### Intent match bonus
- Director/Secretaría: evidencia con patrones explícitos de cargo.
- Duración: evidencia con "duración" + formato años plausible.
- Materias por año: sección del año + lista de materias.

---

## Hechos Estructurados (Facts) Sin Romper El Sistema
`program_facts` debe ser **secundario**, no fuente única.

Solo guardar facts sensibles si:
1. URL canónica válida (`/carreras/` o `/oferta-academica/`).
2. Valor plausible (ej duración entre 1 y 12 años).
3. Contexto semántico correcto (no números sueltos de sitemap).

Si no pasa validaciones: no guardar fact.

---

## Estrategia Para Plan De Estudios (Materias por año)
No extraer por regex global. Hacer parse por secciones:
1. Detectar bloque `Primer/Segundo/Tercer/... Año`.
2. Cortar hasta el próximo año.
3. Extraer `Materia:` / `Asignatura:` con variantes multilinea.
4. Persistir `year_N_subject`.

Cuando el usuario pregunta:
- "materias de segundo año": responder desde `year_2_subject`.
- "decime todas": heredar año desde historial inmediato.

---

## Manejo De Conversación (Follow-ups)
Guardar estado contextual por sesión:
- `active_program`
- `active_year`
- `active_topic` (director, duración, materias, trámites)

Si el usuario dice "decime todas" o "y de tercero?", usar ese estado antes de preguntar de nuevo.

---

## Evitar Latencia Alta
Regla operativa:
- `query` nunca scrapea web en runtime por defecto.
- Scraping solo en jobs explícitos (`crawl/reindex`).
- Si querés "modo verificación live", hacerlo en endpoint aparte con timeout estricto y botón manual.

---

## Contrato De Respuesta (obligatorio)
Para respuestas factuales:
1. Debe incluir URL fuente.
2. Debe venir de `career_canonical`/`offer_canonical` salvo tema administrativo.
3. Si no hay evidencia suficiente:
   - no inventar,
   - devolver: "No hay evidencia suficiente en fuentes canónicas indexadas" + sugerencia de reindex puntual URL.

---

## Dataset De Evaluación (imprescindible)
Crear un set de prueba por perfil:
- 30 preguntas `Ingresantes`
- 30 `Estudiantes`
- 20 `Docentes`
- 20 `NoDocentes`
- 20 `Directivos`

Medir:
- `retrieval@k`
- `answer factual accuracy`
- `citation correctness`
- `conflict handling success`
- `p95 latency`

Sin este benchmark, no se puede afirmar que "funciona".

---

## Plan De Implementación Por Fases

### Fase 1 (rápida, 1-2 días)
- Desactivar runtime scraping en query.
- Filtrar ruido por `page_type`.
- Ranking por autoridad/recencia.
- Facts solo en canónicas + validación de plausibilidad.

### Fase 2 (media, 3-5 días)
- Índice híbrido BM25 + dense + RRF.
- Reranker en top-50 -> top-8.
- Estado conversacional (`active_program`, `active_year`).

### Fase 3 (pro, 1-2 semanas)
- Evaluación automática continua (set QA versionado).
- Panel de auditoría de respuestas (evidencia + score + conflictos).
- Reindex puntual por URL desde dashboard.

---

## Operación Recomendada
- Re-crawl masivo: solo cuando cambie estructura del sitio.
- Reindex puntual: cuando cambia una carrera/página específica.
- Limpieza periódica de facts inválidos.
- Alertas cuando haya conflicto entre fuentes de alta autoridad.

---

## Resumen Ejecutivo
Para este sistema, la mejor estrategia no es "más regex" ni "más chunks".
Es:
1. **Canónicas primero**,
2. **Recuperación híbrida + rerank**,
3. **Evidencia obligatoria**,
4. **Facts validados como apoyo**,
5. **Evaluación objetiva continua**.

Con esto se logra precisión real sin respuestas inventadas ni latencias absurdas.
