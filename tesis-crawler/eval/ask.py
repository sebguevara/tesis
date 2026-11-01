"""
Pregunta rápida al widget local, para smoke tests.

    python eval/ask.py <source_id> "¿Cuánto dura la carrera?" ["seguimiento" ...]

Varias preguntas = misma conversación (mismo session_id).
"""
import json
import sys
import time
import uuid

import httpx

source_id, questions = sys.argv[1], sys.argv[2:]
session_id = str(uuid.uuid4())
for q in questions:
    t = time.time()
    r = httpx.post(
        "http://127.0.0.1:8000/api/widget/query",
        headers={"X-API-Key": "pfc_sk_local_demo_univ_2026_001", "Origin": "http://localhost:5173"},
        json={"question": q, "source_id": source_id, "session_id": session_id},
        timeout=120,
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    answer = body.get("answer") if isinstance(body, dict) else body
    print(f"\n[{time.time() - t:.1f}s] {q}\n→ {answer if answer else json.dumps(body, ensure_ascii=False)[:500]}")
