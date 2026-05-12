import asyncio
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI

# Quiet third-party noise at boot. Set BEFORE importing huggingface_hub /
# transformers / sentence_transformers so they pick these up.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Make app loggers visible under uvicorn (which only configures its own loggers).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

# Silence chatty third-party loggers (httpx cache-revalidation, HF cache probes,
# sentence-transformers boot messages, etc.). Override with LOG_LEVEL_<NAME>=DEBUG
# if you need to diagnose model loading.
for _noisy in (
    "httpx",
    "httpcore",
    "huggingface_hub",
    "huggingface_hub.utils._http",
    "sentence_transformers",
    "sentence_transformers.base.model",
    "sentence_transformers.SentenceTransformer",
    "transformers",
    "urllib3",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api import auth, query, scrape, sources, status, widget
from app.core.reranker import warmup as warmup_reranker
from app.core.widget_origin import is_origin_allowed_globally
from app.storage.db_client import init_db


# Playwright on Windows requires Proactor event loop (subprocess support).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="UNNE Med RAG")
static_dir = Path(__file__).parent / "static"


@app.middleware("http")
async def dynamic_widget_cors(request, call_next):
    path = (request.url.path or "").lower()
    if not path.startswith("/api/widget/"):
        return await call_next(request)

    origin = (request.headers.get("origin") or "").strip()
    if request.method.upper() == "OPTIONS":
        if not origin:
            return JSONResponse({"detail": "Origin header requerido"}, status_code=400)
        if not await is_origin_allowed_globally(origin):
            return JSONResponse({"detail": "Origin no permitido"}, status_code=403)
        response = JSONResponse({"ok": True}, status_code=200)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "POST,GET,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key"
        return response

    response = await call_next(request)
    if origin and await is_origin_allowed_globally(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "POST,GET,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key"
    return response


@app.on_event("startup")
async def startup():
    await init_db()
    # Stage 3+ uses a cross-encoder reranker (~120MB, ~10-20s to load).
    # Warm it up at boot in a thread so the first user query doesn't pay the cold-start.
    asyncio.create_task(asyncio.to_thread(warmup_reranker))


app.include_router(scrape.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(status.router, prefix="/api")
app.include_router(sources.router, prefix="/api")
app.include_router(widget.router, prefix="/api")
app.include_router(auth.router, prefix="/api")

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "API online"}
