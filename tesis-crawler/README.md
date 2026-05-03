# tesis-crawler

Backend FastAPI del trabajo final: crawlea sitios institucionales (caso de prueba: `med.unne.edu.ar`), indexa el contenido con `pgvector` + Postgres FTS, y sirve un widget conversacional sobre un grafo RAG de 4 nodos (rewrite → retrieve → generate → verify).

> Ver [`docs/RAG_ITERATIONS.md`](docs/RAG_ITERATIONS.md) para la bitácora completa de las 5 etapas (baseline → speed-up → contextual → hybrid+rerank → rewrite+verify → limpieza final) con números reales del eval set.

## Stack

- Python 3.12, FastAPI, SQLModel, asyncpg.
- Postgres 17 + extensión `pgvector` (un solo container, sin pgai).
- Crawl4AI 0.8 + Playwright para el crawl institucional.
- LangChain + LangGraph para orquestar el grafo RAG.
- OpenAI: `text-embedding-3-large` (1536-dim) para embeddings; `gpt-4o-mini` como modelo helper (rewrite + verify + contextualization); `OPENAI_CHAT_MODEL` para la respuesta final.
- `sentence-transformers` con `cross-encoder/ms-marco-MiniLM-L-12-v2` para reranking local (sin Cohere).

## Setup local

1. Levantar Postgres con pgvector:
   ```bash
   docker compose up -d
   ```

2. Configurar `.env`. Las claves obligatorias:
   ```env
   DATABASE_URL="postgresql+psycopg://USER:PASS@localhost:6565/tesis"
   POSTGRES_USER=tesis
   POSTGRES_PASSWORD=tesis
   POSTGRES_DB=tesis
   OPENAI_API_KEY=sk-...
   OPENAI_CHAT_MODEL=gpt-4o-mini
   OPENAI_CONTEXT_MODEL=gpt-4o-mini       # Stage 4 helper (rewrite + verify)
   WIDGET_DEV_API_KEY=pfc_sk_local_demo_univ_2026_001
   SITE_MD_DIR=./data/markdown
   RAG_ENABLE_CONTEXTUAL_RETRIEVAL=true   # Stage 2 (default true)
   RAG_GRAPH_TIMEOUT_SECONDS=60           # Stage 4 raised from 25
   ```

3. Instalar deps con `uv`:
   ```bash
   uv sync
   ```

4. Levantar el backend (UTF-8 obligatorio en Windows por el logger de Crawl4AI):
   ```bash
   PYTHONIOENCODING=utf-8 PYTHONUTF8=1 uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
   El primer arranque carga el cross-encoder (~10–20 s). El warmup corre en background así que el `init_db` no se bloquea.

## Crawl institucional

```bash
curl -X POST http://localhost:8000/api/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://med.unne.edu.ar/",
    "max_pages": 400,
    "concurrency": 20,
    "max_depth": 5,
    "use_sitemap_seed": false
  }'
```

Devuelve `{"job_id": "<uuid>", "status": "accepted"}`. Progreso:

```bash
curl http://localhost:8000/api/status/<job_id>
```

`use_sitemap_seed=true` activa la ruta sitemap-first (más rápida, menos recall — ver Etapa 1 en `docs/RAG_ITERATIONS.md`).

## Eval set

Los archivos del eval están en [`eval/`](eval/):
- `eval_set.json`: 50 ítems anclados en hechos verificados de `med.unne.edu.ar` al 02-05-2026, distribuidos en 10 categorías (`factual_simple`, `listing`, `requirements`, `authority`, `dates`, `contact`, `conversational`, `typo_robust`, `ambiguous`, `out_of_scope`). 10 ítems están agrupados por `conversation_id` para probar multiturno.
- `run_eval.py`: ejecuta los 50 ítems contra `/api/widget/query` (envía `debug: true` para que la API devuelva los `context_chunks` recuperados).
- `score_eval.py`: LLM-as-judge con `gpt-4o` (override con `--judge-model`). Calcula `correctness`, `faithfulness`, `hallucination`, y para `ambiguous`/`out_of_scope` también `refusal_correct`. Agrega métricas globales y por categoría.

### Correr el eval completo

```bash
# 1. Asegurate de que el crawl ya terminó y el backend está corriendo.
# 2. Exportar credenciales:
export BASE_URL=http://127.0.0.1:8000
export API_KEY=pfc_sk_local_demo_univ_2026_001        # WIDGET_DEV_API_KEY
export SOURCE_ID=$(curl -s "$BASE_URL/api/sources/lookup?domain=med.unne.edu.ar" \
  | python -c "import json,sys; print(json.load(sys.stdin)['source_id'])")
export OPENAI_API_KEY=sk-...

# 3. Run + score:
cd eval
python run_eval.py --eval-set eval_set.json --output results/05_final.json
python score_eval.py --results results/05_final.json --output results/scored_05_final.json
```

Hay también un wrapper `eval/run_baseline.sh` que automatiza esto.

### Métricas finales (Etapa 5, sobre `med.unne.edu.ar`, judge `gpt-4o`)

| Métrica | Etapa 0 baseline | **Etapa 5 final** | Mejora |
|---|---|---|---|
| `correctness_avg` | 0.389 | **0.672** | **+73% rel** |
| `faithfulness_avg` | 0.789 | **1.000** | **+27% rel** (perfecto) |
| `hallucination_rate` | 0.180 | **0.040** | **-78% rel** ✅ (objetivo `< 0.10`) |
| `refusal_correct_rate` | 0.600 | 0.800 | +33% rel |

**9 de 10 categorías** llegan a **0% hallucination**: `ambiguous`, `authority`, `contact`, `conversational`, `dates`, `listing`, `out_of_scope`, `requirements`, `typo_robust`. Solo `factual_simple` queda en 0.167 (2/12 — uno es retrieval ambiguo de Q003 presencial/virtual y otro es falso positivo del judge en Q005 POF horas que en realidad responde correcto).

## Arquitectura del grafo RAG (Etapas 3 + 4)

```
state["query"] ──▶ [rewrite (gpt-4o-mini si hay history)] ──▶ state["resolved_query"]
                                                                       │
                                                                       ▼
                   [retrieve (pgvector top-30 + FTS top-30)
                    → RRF top-50 → cross-encoder top-8] ──▶ state["context"]
                                                                       │
                                                                       ▼
                   [generate (OPENAI_CHAT_MODEL con SYSTEM_RAG)] ──▶ state["response"]
                                                                       │
                                                                       ▼
                   [verify (gpt-4o-mini, score < 0.6 → decline)] ──▶ state["response"]'
```

El código está en [`app/core/rag_service.py`](app/core/rag_service.py) (525 líneas tras la limpieza de Etapa 5; antes eran 4234 con fast-paths regex / URL hints / fact extraction / live-fetch).

## Endpoints relevantes

- `POST /api/scrape` — dispara un crawl en background.
- `GET /api/status/{job_id}` — progreso y métricas del crawl.
- `GET /api/sources` — lista de sources crawleados.
- `GET /api/sources/lookup?domain=...` — buscar `source_id` por dominio.
- `POST /api/widget/query` — query del widget. Body: `{"question", "source_id", "session_id?", "debug?"}`. Con `debug: true` devuelve `context_chunks`.

## Convenciones del repo

- Branch por etapa (`stage-N-descripcion`), merge a `master` solo después de validar con el eval.
- Cada etapa documenta resultados, intentos descartados y trade-offs en [`docs/RAG_ITERATIONS.md`](docs/RAG_ITERATIONS.md).
- No se commitea código que no haya pasado el smoke test (`run_eval.py` + `score_eval.py`).
