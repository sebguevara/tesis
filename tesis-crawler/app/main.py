import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.api import query, scrape, sources, status, widget
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
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key,X-Admin-Token"
        return response

    response = await call_next(request)
    if origin and await is_origin_allowed_globally(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "POST,GET,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-Key,X-Admin-Token"
    return response


@app.on_event("startup")
async def startup():
    await init_db()


app.include_router(scrape.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(status.router, prefix="/api")
app.include_router(sources.router, prefix="/api")
app.include_router(widget.router, prefix="/api")

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def index():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "API online"}
