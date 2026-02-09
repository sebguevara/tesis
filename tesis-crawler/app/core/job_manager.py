import uuid
from datetime import datetime
from typing import Dict, Literal, Optional

from pydantic import BaseModel, Field


class JobProgress(BaseModel):
    job_id: str
    status: Literal["pending", "running", "completed", "failed"]
    phase: str = "pending"
    message: str = "En cola"
    progress_pct: float = 0.0
    eta_seconds: Optional[int] = None
    pages_crawled: int = 0
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    last_updated_at: datetime = Field(default_factory=datetime.now)
    metrics: dict = Field(
        default_factory=lambda: {
            "total_results": 0,
            "successful_results": 0,
            "saved_docs": 0,
            "saved_markdown_files": 0,
            "skipped_invalid_content": 0,
            "skipped_ingestion": 0,
            "skipped_save_markdown": 0,
            "skipped_processing_errors": 0,
            "skipped_db_disabled": 0,
            "blocked_by_host_filter": 0,
            "blocked_by_allow_filter": 0,
            "matched_allow_filter": 0,
            "blocked_by_block_filter": 0,
        }
    )


class JobManager:
    _instance = None
    _jobs: Dict[str, JobProgress] = {}

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def create_job(self) -> str:
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = JobProgress(job_id=job_id, status="pending")
        return job_id

    def update_job(self, job_id: str, **kwargs):
        if job_id in self._jobs:
            current = self._jobs[job_id].model_dump()
            current.update(kwargs)
            current["last_updated_at"] = datetime.now()
            self._jobs[job_id] = JobProgress(**current)

    def increment_metric(self, job_id: str, key: str, amount: int = 1):
        if job_id not in self._jobs:
            return
        current = self._jobs[job_id].model_dump()
        metrics = current.get("metrics", {})
        metrics[key] = metrics.get(key, 0) + amount
        current["metrics"] = metrics
        self._jobs[job_id] = JobProgress(**current)

    def get_job(self, job_id: str) -> Optional[JobProgress]:
        return self._jobs.get(job_id)


job_manager = JobManager()
