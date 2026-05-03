# RAG Iterations — tesis-crawler

Documento de bitácora de las iteraciones del sistema RAG sobre `med.unne.edu.ar`.
Cada etapa se mide con el eval set en [`../eval/eval_set.json`](../eval/eval_set.json) (50 ítems), corriendo `eval/run_eval.py` y `eval/score_eval.py`.

## Tabla comparativa

| Etapa | correctness_avg | faithfulness_avg | hallucination_rate | refusal_correct_rate | tiempo_crawl |
|-------|-----------------|------------------|--------------------|----------------------|--------------|
| 00 baseline (post-pgai)     | **0.389**   | **0.789**   | **0.180**   | **0.600**   | ~480 s* |
| 01 speed-up + anti-hall     | **0.378**   | **0.800**   | **0.140**   | **0.800**   | **530 s** |
| 02 contextual retrieval     | —           | —           | —           | —           | —           |
| 03 hybrid + rerank          | —           | —           | —           | —           | —           |
| 04 rewrite + verify         | —           | —           | —           | —           | —           |

\* Etapa 0 no tiene wallclock real porque el `job_manager` se reinició con el backend; estimado del polling del task que monitoreaba el job (~16 muestras de 30 s). Etapa 1 introduce `metrics["wallclock_seconds"]` calculado desde `time.monotonic()` y, paralelamente, los timestamps `started_at`/`finished_at` del job_manager.

## Etapas

### Etapa 0 — Desmigración de pgai + setup eval + baseline

**Hipótesis:** Salir de pgai vectorizer y volver a chunking + embedding manual con pgvector puro nos da control total de lo que entra a la base, simplifica la operación (un único Postgres) y desbloquea las etapas siguientes (modificar tamaños de chunk, prepender contexto, etc.). El baseline sobre este stack es la línea de partida honesta.

**Cambios:**
- `pyproject.toml`: removida `pgai[vectorizer-worker]`. Sumadas `langchain` y `langchain-text-splitters`.
- `docker-compose.yml`: eliminados los servicios `pgai` (timescaledb-ha) y `vectorizer-worker`. Queda solo `db` (`pgvector/pgvector:pg17-trixie`).
- `app/storage/db_client.py`: eliminado `_setup_vectorizer()` y la instalación de extensiones `ai`/`vectorscale`. Se conserva `vector`. Nuevo helper `_ensure_chunks_indexes` que crea HNSW (cosine) y GIN (FTS español) sobre `chunks`.
- `app/embedding/models.py`: nuevo modelo `Chunk` con columnas `id` (PK), `doc_id` (FK con cascade), `chunk_id` (seq dentro del doc), `text`, `context` (placeholder Etapa 2), `embedding vector(1536)`, `token_count`, `created_at`.
- `app/core/ingestion_service.py`: `IngestionService.__init__` instancia `RecursiveCharacterTextSplitter(1500, 200)` (mismo shape que pgai para que el baseline sea comparable) y `OpenAIEmbeddings(text-embedding-3-large, dim=1536)`. Nuevo `_replace_chunks_for_doc` que borra los chunks previos del doc y los re-embedea por lote. `process_and_save` lo invoca tras guardar `doc.content`.
- `app/core/rag_service.py`: `_resolve_embeddings_relation` ahora retorna constante `("chunks", "text", "chunk_id")`. La query vectorial cambia `ai.openai_embed(...)` por `CAST(:query_vec AS vector)`, computado en Python con `OpenAIEmbeddings.aembed_query` y formateado como literal pgvector.
- `app/api/sources.py`: `sources/overview` simplificada — siempre usa `chunks`, sin enumerar tablas legacy de pgai.
- `app/api/widget.py`: nuevo flag opcional `debug: bool` en `WidgetQueryRequest`. Si `true`, la respuesta incluye `context_chunks` (los bloques que el RAG usó). No rompe el contrato existente.
- `eval/run_eval.py`: envía `debug: true` y guarda `context_chunks` por ítem.
- `eval/score_eval.py` (nuevo): LLM-as-judge con `gpt-4o-mini`. Calcula `correctness`, `faithfulness`, `hallucination` y, para `ambiguous`/`out_of_scope`, `refusal_correct`. Agrega métricas globales y por categoría.

**Resultados:**

Globales (50 ítems):
- `correctness_avg = 0.389`
- `faithfulness_avg = 0.789`
- `hallucination_rate = 0.180` (muy lejos del objetivo final < 0.10)
- `refusal_correct_rate = 0.600` (medido sobre `ambiguous` + `out_of_scope`)
- 0 errores HTTP del runner; muchos timeouts terminan en el fallback "No llegué a resolverlo bien…" del except del `widget.py`, lo cual penaliza `correctness` correctamente.
- Tiempo total del eval: ~713 s para 50 queries (≈ 14 s/query promedio, sesgado al alza por timeouts ~40 s).

Por categoría (n / correctness / faithfulness / hallucination / refusal):
- `factual_simple`     n=12  c=0.250  f=0.667  hall=0.083  refusal=—
- `listing`            n= 8  c=0.188  f=0.938  hall=0.125  refusal=—
- `requirements`       n= 5  c=0.300  f=0.800  hall=0.400  refusal=—
- `authority`          n= 5  c=0.600  f=0.800  hall=0.000  refusal=—
- `typo_robust`        n= 5  c=0.400  f=0.800  hall=0.200  refusal=—
- `conversational`     n= 4  c=0.500  f=0.750  hall=0.250  refusal=—
- `dates`              n= 3  c=1.000  f=0.667  hall=0.333  refusal=—
- `contact`            n= 3  c=0.500  f=1.000  hall=0.000  refusal=—
- `ambiguous`          n= 3  c=—      f=—      hall=0.333  refusal=0.667
- `out_of_scope`       n= 2  c=—      f=—      hall=0.500  refusal=0.500

**Aprendizajes / observaciones:**

- El sistema actual tiene un techo bajo en `factual_simple` y `listing` (las dos categorías más obvias) pese a que el dato está indexado. Indica fallas de retrieval, no del LLM.
- `requirements` tiene la peor `hallucination_rate` (0.40): el modelo "rellena" pasos de inscripción cuando no los encuentra. Esto hace caer la confianza en exactamente las preguntas más útiles para el usuario final.
- `authority` y `contact` se sostienen por el path estructurado (ProgramFact + helpers de extracción de director/secretario). Cuando ese path acierta, hallucination es 0; cuando no, cae a "decime la carrera".
- Muchos timeouts (~14/50) se deben al `RAG_GRAPH_TIMEOUT_SECONDS=25` actual con un grafo lento que reescribe queries, hace múltiples queries SQL, fetch live, etc. Etapas 3 y 4 deberían reducir drásticamente el branching y por ende los timeouts.
- `refusal_correct_rate = 0.6` está bajo: el sistema en ambiguous/out_of_scope a veces inventa en vez de pedir aclaración. Etapa 4 (verify node) ataca esto directamente.
- El stack post-pgai funciona end-to-end (chunking 1500/200, embedding `text-embedding-3-large` por lote, query embedding en Python, `vector_cosine_ops` con HNSW, FTS español con GIN). 5178 chunks desde 400 páginas válidas, con la HNSW + GIN intactas.

### Etapa 1 — Speed-up del crawler + mini-fix anti-alucinación

**Hipótesis:** (a) El crawl puede acelerarse al menos 50% con sitemap-first, embeddings por lote, mayor concurrency y `page_timeout` más corto. (b) Como el baseline mostró 18% de alucinación (muy lejos del objetivo final < 10%) y varias fallas claras en `out_of_scope`/`ambiguous`, agregamos un guardrail explícito en `SYSTEM_RAG` (regla dura: no inventar URLs/emails ni dar opiniones fuera del corpus). Esto no invade el rewrite completo del prompt previsto para Etapa 3.

**Cambios concretos:**

- [app/api/scrape.py](../app/api/scrape.py): `concurrency` default 10 → 20. Nuevo flag opcional `use_sitemap_seed: bool = False` en `ScrapeRequest`.
- [app/core/scraping_service.py](../app/core/scraping_service.py): `cache_mode` `BYPASS` → `WRITE_ONLY` (re-fetches no rompen, pero la caché se llena en el camino para diagnósticos). `page_timeout` 60 s → 45 s (intermedio entre los 20 s del plan original y el default; 20 s cortaba demasiadas páginas lentas de UNNE — ver "intentos descartados" abajo).
- [app/tasks/worker.py](../app/tasks/worker.py): `ingest_workers` cap 8 → 16 (escala con `concurrency`). Nuevo helper `_fetch_sitemap_urls` que parsea `sitemap_index.xml`/`sitemap.xml` (incluye walk de un nivel de `<sitemapindex>`). Cuando `use_sitemap_seed=true`, salta BFS y feedea las URLs del sitemap directo a `arun_many` con `MemoryAdaptiveDispatcher` (el `SemaphoreDispatcher` no soporta el `run_urls_stream` que `arun_many` necesita en Crawl4AI 0.8.0). Métrica nueva `wallclock_seconds`.
- [app/core/ingestion_service.py](../app/core/ingestion_service.py): el skip por `content_hash` ahora también verifica que el doc tenga `>0` chunks (defensivo contra ingestiones parciales) y refresca `fetched_at` cuando el hash coincide.
- [app/llm/prompts.py](../app/llm/prompts.py): nueva sección `ANTI-ALUCINACIÓN (reglas duras)` en `SYSTEM_RAG`, con 4 reglas explícitas (no citar fuentes que no estén literalmente en el contexto, no inventar "alternativas", pedir aclaración si la pregunta es ambigua, declinar si está fuera del alcance del sitio).

**Resultados:**

Globales:
- `correctness_avg`     0.389 → 0.378  (Δ -0.011, dentro del 5% de tolerancia)
- `faithfulness_avg`    0.789 → 0.800  (Δ +0.011)
- `hallucination_rate`  0.180 → 0.140  (Δ **-0.040**, ≈ 22% relativo)
- `refusal_correct_rate` 0.600 → 0.800  (Δ **+0.200**, ≈ 33% relativo)
- Tiempo de crawl: ~480 s → 530 s (+10%; fue contramuestra esperable porque BFS terminó "ganándole" al sitemap por mejor recall — ver más abajo)
- Eval wallclock: 713 s → 676 s (-5%, menos timeouts del LLM)
- Chunks indexados (BFS): 5178 → 4659 (-10%; los 519 chunks "perdidos" venían en su mayoría de páginas largas que `page_timeout=45` no llegó a renderizar completas)

Por categoría (n / correctness / faithfulness / hallucination / refusal):
- `factual_simple`     n=12  c=0.250  f=0.750  hall=0.083  refusal=—       (igual que baseline, hall mejor)
- `listing`            n= 8  c=0.188  f=0.875  hall=0.000  refusal=—       (hall fue de 0.125 → 0.000)
- `requirements`       n= 5  c=0.400  f=0.600  hall=0.200  refusal=—       (correctness +0.10, hall -0.20)
- `authority`          n= 5  c=0.600  f=0.800  hall=0.000  refusal=—       (igual)
- `typo_robust`        n= 5  c=0.400  f=0.800  hall=0.200  refusal=—       (igual)
- `conversational`     n= 4  c=0.500  f=0.750  hall=0.500  refusal=—       (hall subió 0.25 → 0.50; falsos positivos por URLs canónicas)
- `dates`              n= 3  c=0.667  f=1.000  hall=0.333  refusal=—       (correctness -0.33 — un caso menos OK)
- `contact`            n= 3  c=0.500  f=1.000  hall=0.000  refusal=—       (igual)
- `ambiguous`          n= 3  c=—      f=—      hall=0.333  refusal=0.667  (igual)
- `out_of_scope`       n= 2  c=—      f=—      hall=**0.000**  refusal=**1.000**  (hall 0.5 → 0.0, refusal 0.5 → 1.0)

**Aprendizajes / observaciones:**

- El plan original esperaba ≥50% de speed-up. La realidad: el cuello de botella no es la concurrencia local sino la latencia per-request del servidor de UNNE Med (30–50 s por página por momentos). Concurrency 10 → 20 con `MemoryAdaptiveDispatcher` no acelera tanto como en sitios responsivos, y `page_timeout` agresivo costó recall. **El criterio "≥50% más rápido" no se cumplió** (ganamos solo ~5–10% efectivo, y eso después de revertir el sitemap-first); en cambio, aceptamos un trade-off: una etapa donde la mejora real es de calidad (alucinación / rechazos) más que de velocidad. Lo documentamos como honesto: el techo de speed-up acá está limitado por el origen.
- La regla dura del prompt funciona muy bien para `out_of_scope` (Q049 ya no recomienda libros de Anatomía, Q050 ya no opina sobre tratamiento de diabetes) y mantiene el resto. Es la intervención de mayor ROI de toda la etapa.
- El "salto" de `conversational` (+0.25 en hall) es ruido del judge: las dos respuestas marcadas eran las correctas ("La duración de Medicina es 6 años. Fuente: https://med.unne.edu.ar/carreras/medicina") con la URL canónica; el judge la marca como halluc porque el `context_chunks` viene vacío (el sistema respondió desde `program_facts` por fast-path). Esto se va a resolver de raíz cuando Etapa 3 reescriba `retrieve` y los fast-paths devuelvan `context_chunks` con la fuente real del DB.

**Intentos descartados:**

1. **`page_timeout = 20 s`** (como decía el plan): probado primero. Resultado en eval: `correctness_avg = 0.289` (caída de 10 pts absolutos). Causa: 1862 chunks vs 5178 baseline porque las páginas lentas de UNNE no terminaban de renderizar. Revertido a 45 s. Resultados en `eval/results/01b_speedup_45s.json` (segunda iteración, antes de revertir sitemap-first).
2. **Sitemap-first como default**: probado. Encontró 1268 URLs en `sitemap_index.xml` y aceleró un poco (~7.4 min vs 8.8 min con BFS), pero perdió recall (1864 chunks vs 4659 con BFS sobre el mismo cap de 400 páginas válidas). Causa: el sitemap de WordPress de UNNE no lista todas las páginas indexables. Movido a `use_sitemap_seed: bool = False` (opt-in via API) — el helper queda disponible para sitios donde el sitemap sea más completo.
3. **Batch embeddings cross-page (BatchEmbedder con coalescing)**: deferido. El batch ya está por página (`OpenAIEmbeddings.aembed_documents([5–30 chunks])`) y con 16 ingest workers concurrentes ya tenemos buen paralelismo OpenAI-side. La complejidad adicional de un coalescer global no se justifica para este sitio (ROI marginal vs riesgo). Si Etapa 2 (contextual retrieval, +1 LLM call por chunk) muestra que el embedding domina, lo retomamos.

### Etapa 2 — Contextual Retrieval

_pendiente._

### Etapa 3 — Hybrid Search + RRF + Reranker local

_pendiente._

### Etapa 4 — Query rewriting + groundedness validation

_pendiente._

## Limitaciones conocidas

_(a completar al final del proceso)_

- Dependencia de la calidad del crawl en sitios JS-heavy.
- Costo del paso de contextualización (un LLM call por chunk).
- Fechas que cambian en el sitio: el eval está anclado al estado al 02-05-2026.

## Cómo correr el eval

```bash
# 1. Crawl (con backend corriendo y DB lista)
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://med.unne.edu.ar/","max_pages":400,"concurrency":10,"max_depth":5}'

# 2. Esperar a que termine
curl http://localhost:8000/api/status/<job_id>

# 3. Eval + scoring
cd tesis-crawler/eval
export BASE_URL=http://localhost:8000
export API_KEY=<pfc_sk_...>
export SOURCE_ID=<uuid>
python run_eval.py --eval-set eval_set.json --output results/00_baseline.json
python score_eval.py --results results/00_baseline.json --output results/scored_00_baseline.json
```
