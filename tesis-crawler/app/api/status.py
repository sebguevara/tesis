import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from app.core.job_manager import job_manager

router = APIRouter(tags=["Status"])


def _build_status_payload(job) -> dict:
    now = datetime.now()
    metrics = dict(job.metrics or {})
    started_at = job.started_at
    last_updated_at = job.last_updated_at

    elapsed_seconds = 0.0
    if started_at:
        elapsed_seconds = max(0.0, (now - started_at).total_seconds())

    freshness_ms = 0
    if last_updated_at:
        freshness_ms = max(0, int((now - last_updated_at).total_seconds() * 1000))

    processed_results = int(metrics.get("processed_results", 0) or 0)
    saved_docs = int(metrics.get("saved_docs", 0) or 0)
    accepted_valid_pages = int(metrics.get("accepted_valid_pages", 0) or 0)

    processed_per_sec = (processed_results / elapsed_seconds) if elapsed_seconds > 0 else 0.0
    saved_per_sec = (saved_docs / elapsed_seconds) if elapsed_seconds > 0 else 0.0
    valid_per_sec = (accepted_valid_pages / elapsed_seconds) if elapsed_seconds > 0 else 0.0

    ingest_queue_size = int(metrics.get("ingest_queue_size", 0) or 0)
    ingest_inflight = int(metrics.get("ingest_inflight", 0) or 0)
    ingest_pending_total = int(metrics.get("ingest_pending_total", 0) or (ingest_queue_size + ingest_inflight))

    is_running = job.status in {"running", "pending"}
    is_stale = bool(is_running and freshness_ms > 12000)
    in_discovery = bool(is_running and processed_results == 0)

    payload = job.model_dump()
    payload["metrics"] = metrics
    payload["telemetry"] = {
        "server_time": now.isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 2),
        "freshness_ms": freshness_ms,
        "is_stale": is_stale,
        "processed_per_sec": round(processed_per_sec, 3),
        "saved_per_sec": round(saved_per_sec, 3),
        "valid_per_sec": round(valid_per_sec, 3),
        "current_url": str(metrics.get("last_processed_url") or ""),
        "ingest_queue_size": ingest_queue_size,
        "ingest_inflight": ingest_inflight,
        "ingest_pending_total": ingest_pending_total,
        "ingest_workers": int(metrics.get("ingest_workers", 0) or 0),
        "update_seq": int(payload.get("update_seq", 0) or 0),
        "in_discovery": in_discovery,
        "discovery_note": (
            "Descubriendo enlaces iniciales; las tasas aparecen al procesar la primera página."
            if in_discovery
            else ""
        ),
    }
    return payload


@router.get("/status/{job_id}")
async def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _build_status_payload(job)


@router.get("/status/{job_id}/stream")
async def stream_job_status(job_id: str):
    if not job_manager.get_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        last_seq = -1
        heartbeat_every = 5
        idle_ticks = 0
        while True:
            job = job_manager.get_job(job_id)
            if not job:
                payload = {"detail": "Job not found"}
                yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                break

            payload = _build_status_payload(job)
            seq = int(payload.get("update_seq", 0) or 0)
            if seq != last_seq:
                encoded = jsonable_encoder(payload)
                yield f"event: status\ndata: {json.dumps(encoded, ensure_ascii=False)}\n\n"
                last_seq = seq
                idle_ticks = 0
            else:
                idle_ticks += 1
                if idle_ticks >= heartbeat_every:
                    idle_ticks = 0
                    yield f": keepalive {datetime.now().isoformat()}\n\n"

            if job.status in {"completed", "failed"}:
                break
            await asyncio.sleep(1.0)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.get("/status/{job_id}/metrics")
async def get_job_metrics(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": job.status, "metrics": job.metrics}


@router.get("/status/{job_id}/filters")
async def get_job_filter_stats(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    metrics = job.metrics or {}
    return {
        "job_id": job_id,
        "status": job.status,
        "blocked_by_host_filter": metrics.get("blocked_by_host_filter", 0),
        "blocked_by_allow_filter": metrics.get("blocked_by_allow_filter", 0),
        "matched_allow_filter": metrics.get("matched_allow_filter", 0),
        "blocked_by_block_filter": metrics.get("blocked_by_block_filter", 0),
    }
