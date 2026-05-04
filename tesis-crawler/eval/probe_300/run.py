"""
Run 300-question probe against the live RAG pipeline.

Usage (from tesis-crawler/):
    uv run python eval/probe_300/run.py

Saves results incrementally to eval/probe_300/results.jsonl so partial
progress is preserved if the run is interrupted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Allow `python eval/probe_300/run.py` when invoked from the project root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.rag_service import RAGService  # noqa: E402

HERE = Path(__file__).parent
QUESTIONS_PATH = HERE / "questions.json"
RESULTS_PATH = HERE / "results.jsonl"
SUMMARY_PATH = HERE / "summary.json"

CONCURRENCY = 3
PER_QUESTION_TIMEOUT = 240.0  # seconds

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("openai").setLevel(logging.ERROR)


def looks_like_decline(text: str) -> bool:
    if not text:
        return True
    low = text.lower()
    markers = (
        "no encontré evidencia",
        "no encontre evidencia",
        "no tengo información",
        "no tengo informacion",
        "no llegué a resolverlo",
        "no llegue a resolverlo",
        "no es algo que yo pueda responder",
    )
    return any(m in low for m in markers)


async def run_one(rag: RAGService, source_id: str, item: dict, sem: asyncio.Semaphore) -> dict:
    async with sem:
        graph = rag.build_graph()
        t0 = time.perf_counter()
        payload = {
            "query": item["q"],
            "context": [],
            "response": "",
            "history": [],
            "source_id": source_id,
            "session_state": {},
        }
        try:
            res = await asyncio.wait_for(graph.ainvoke(payload), timeout=PER_QUESTION_TIMEOUT)
            dt = time.perf_counter() - t0
            answer = (res.get("response") or "").strip()
            ground = res.get("groundedness")
            unsupp = res.get("unsupported_claims") or []
            decline = looks_like_decline(answer)
            expects_decline = bool(item.get("expects_decline"))
            # success policy:
            #  - out-of-scope/decline question → success iff system declined or answered safely
            #  - normal question → success iff system did NOT fall back into the decline message
            if expects_decline:
                ok = decline or "no es algo que yo pueda responder" in answer.lower()
            else:
                ok = not decline
            return {
                "id": item["id"],
                "category": item["category"],
                "q": item["q"],
                "expects_decline": expects_decline,
                "answer": answer,
                "groundedness": ground,
                "unsupported_claims": unsupp,
                "duration_s": round(dt, 2),
                "decline": decline,
                "ok": bool(ok),
            }
        except asyncio.TimeoutError:
            return {
                "id": item["id"],
                "category": item["category"],
                "q": item["q"],
                "expects_decline": bool(item.get("expects_decline")),
                "answer": "",
                "groundedness": None,
                "unsupported_claims": [],
                "duration_s": PER_QUESTION_TIMEOUT,
                "decline": True,
                "ok": False,
                "error": "timeout",
            }
        except Exception as exc:
            return {
                "id": item["id"],
                "category": item["category"],
                "q": item["q"],
                "expects_decline": bool(item.get("expects_decline")),
                "answer": "",
                "groundedness": None,
                "unsupported_claims": [],
                "duration_s": round(time.perf_counter() - t0, 2),
                "decline": True,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }


async def main():
    data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    source_id = data["source_id"]
    questions = data["questions"]
    print(f"Total questions: {len(questions)}  concurrency={CONCURRENCY}")

    # resume support: skip ids already in results.jsonl
    done_ids: set[int] = set()
    if RESULTS_PATH.exists():
        for line in RESULTS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                done_ids.add(int(rec["id"]))
            except Exception:
                continue
        if done_ids:
            print(f"Resuming: {len(done_ids)} already done, {len(questions) - len(done_ids)} pending")

    pending = [q for q in questions if q["id"] not in done_ids]
    if not pending:
        print("Nothing to do.")
        return

    rag = RAGService()
    sem = asyncio.Semaphore(CONCURRENCY)

    completed = len(done_ids)
    total = len(questions)
    t_start = time.perf_counter()

    # write results as they finish so we don't lose partial progress
    out = RESULTS_PATH.open("a", encoding="utf-8")
    try:
        tasks = [asyncio.create_task(run_one(rag, source_id, q, sem)) for q in pending]
        for fut in asyncio.as_completed(tasks):
            rec = await fut
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            completed += 1
            mark = "OK " if rec["ok"] else "FAIL"
            elapsed = time.perf_counter() - t_start
            rate = completed / elapsed if elapsed > 0 else 0
            eta = (total - completed) / rate if rate > 0 else 0
            print(
                f"[{completed}/{total}] {mark} #{rec['id']} {rec['category']:<22}"
                f" g={rec['groundedness']} t={rec['duration_s']}s  | "
                f"elapsed={int(elapsed)}s eta={int(eta)}s"
            )
    finally:
        out.close()


if __name__ == "__main__":
    asyncio.run(main())
