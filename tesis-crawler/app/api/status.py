from fastapi import APIRouter, HTTPException
from app.core.job_manager import job_manager

router = APIRouter(tags=["Status"])


@router.get("/status/{job_id}")
async def get_job_status(job_id: str):
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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
