"""
Re-run only the 63 questions that failed in the first probe, after applying
the verify/system prompt fixes. Writes to results_v2.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util
spec = importlib.util.spec_from_file_location("base_runner", Path(__file__).parent / "run.py")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

from app.core.rag_service import RAGService

HERE = Path(__file__).parent
QUESTIONS_PATH = HERE / "questions.json"
FAILURES_PATH = HERE / "failures.json"
RESULTS_V2 = HERE / "results_v2.jsonl"

CONCURRENCY = 6


async def main():
    fails = json.loads(FAILURES_PATH.read_text(encoding="utf-8"))
    fail_ids = {f["id"] for f in fails}
    data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    source_id = data["source_id"]
    pending = [q for q in data["questions"] if q["id"] in fail_ids]
    print(f"Re-running {len(pending)} previously-failed questions")

    rag = RAGService()
    sem = asyncio.Semaphore(CONCURRENCY)
    out = RESULTS_V2.open("w", encoding="utf-8")
    t_start = time.perf_counter()
    completed = 0
    try:
        tasks = [asyncio.create_task(base.run_one(rag, source_id, q, sem)) for q in pending]
        for fut in asyncio.as_completed(tasks):
            rec = await fut
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            completed += 1
            mark = "OK " if rec["ok"] else "FAIL"
            print(
                f"[{completed}/{len(pending)}] {mark} #{rec['id']} {rec['category']:<22} g={rec['groundedness']}"
            )
    finally:
        out.close()
    print(f"Total elapsed: {int(time.perf_counter() - t_start)}s")


if __name__ == "__main__":
    asyncio.run(main())
