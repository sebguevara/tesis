# RAG Iterations — tesis-crawler

Documento de bitácora de las iteraciones del sistema RAG sobre `med.unne.edu.ar`.
Cada etapa se mide con el eval set en [`../eval/eval_set.json`](../eval/eval_set.json) (50 ítems), corriendo `eval/run_eval.py` y `eval/score_eval.py`.

## Tabla comparativa

| Etapa | correctness_avg | faithfulness_avg | hallucination_rate | refusal_correct_rate | tiempo_crawl |
|-------|-----------------|------------------|--------------------|----------------------|--------------|
| 00 baseline (post-pgai)     | **0.389**   | **0.789**   | **0.180**   | **0.600**   | n/a* |
| 01 speed-up                 | —           | —           | —           | —           | —           |
| 02 contextual retrieval     | —           | —           | —           | —           | —           |
| 03 hybrid + rerank          | —           | —           | —           | —           | —           |
| 04 rewrite + verify         | —           | —           | —           | —           | —           |

\* El crawl de Etapa 0 tardó ~17 min por 400 páginas / concurrencia 10 (medición wallclock incompleta — Etapa 1 introduce un cronómetro propio).

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

### Etapa 1 — Speed-up del crawler

_pendiente._

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
