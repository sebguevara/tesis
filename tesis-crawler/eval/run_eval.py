#!/usr/bin/env python3
"""
Runner del eval set contra la API de tesis-crawler.

Uso:
    python run_eval.py \\
        --eval-set eval_set.json \\
        --output results/run_$(date +%Y%m%d_%H%M%S).json \\
        --base-url http://localhost:8000 \\
        --api-key pfc_sk_local_demo_univ_2026_001 \\
        --source-id 79bbfcbd-bf6d-4988-8dd0-6e619287630e

También admite variables de entorno: BASE_URL, API_KEY, SOURCE_ID.

Soporta conversaciones multiturno: ítems con el mismo `conversation_id`
comparten `session_id` y se ejecutan en orden por `turn_number`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def load_eval_set(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def group_items(items: list[dict]) -> list[list[dict]]:
    """
    Devuelve los ítems agrupados por conversation_id (orden por turn_number),
    con las preguntas standalone como grupos de 1 elemento. El orden de los
    grupos sigue el primer ítem de cada uno.
    """
    by_conv: dict[str | None, list[dict]] = defaultdict(list)
    order: list[str | None] = []
    for item in items:
        cid = item.get("conversation_id")
        key = cid if cid else f"_single_{item['id']}"
        if key not in by_conv:
            order.append(key)
        by_conv[key].append(item)
    groups: list[list[dict]] = []
    for key in order:
        group = sorted(
            by_conv[key],
            key=lambda i: (i.get("turn_number") or 0),
        )
        groups.append(group)
    return groups


def call_widget_query(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    source_id: str,
    question: str,
    session_id: str | None,
    timeout: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Devuelve (response_json, error_message). Si error_message es None,
    la llamada fue exitosa.
    """
    url = base_url.rstrip("/") + "/api/widget/query"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "Origin": "http://localhost:5173",
    }
    payload: dict[str, Any] = {
        "question": question,
        "source_id": source_id,
        "debug": True,
    }
    if session_id:
        payload["session_id"] = session_id

    try:
        resp = client.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError as e:
        return None, f"request_error: {e}"

    if resp.status_code >= 400:
        return None, f"http_{resp.status_code}: {resp.text[:300]}"

    try:
        return resp.json(), None
    except json.JSONDecodeError as e:
        return None, f"json_decode_error: {e}"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--eval-set", default="eval_set.json", type=Path, help="Path al eval set JSON")
    p.add_argument("--output", required=True, type=Path, help="Path al archivo de salida JSON")
    p.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    p.add_argument("--api-key", default=os.environ.get("API_KEY", ""))
    p.add_argument("--source-id", default=os.environ.get("SOURCE_ID", ""))
    p.add_argument("--limit", type=int, default=0, help="Limitar a N ítems (0 = todos)")
    p.add_argument("--ids", default="", help="IDs separados por coma para filtrar (ej: Q001,Q005)")
    p.add_argument("--timeout", type=float, default=60.0, help="Timeout por request en segundos")
    p.add_argument("--sleep", type=float, default=0.3, help="Pausa entre requests en segundos")
    p.add_argument("--dry-run", action="store_true", help="No llamar a la API, solo mostrar plan")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.api_key and not args.dry_run:
        print("ERROR: --api-key o env API_KEY requerido", file=sys.stderr)
        return 2
    if not args.source_id and not args.dry_run:
        print("ERROR: --source-id o env SOURCE_ID requerido", file=sys.stderr)
        return 2

    if not args.eval_set.exists():
        print(f"ERROR: no existe {args.eval_set}", file=sys.stderr)
        return 2

    dataset = load_eval_set(args.eval_set)
    items = dataset["items"]

    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        items = [i for i in items if i["id"] in wanted]

    if args.limit > 0:
        items = items[: args.limit]

    groups = group_items(items)

    print(f"Eval set:    {args.eval_set}")
    print(f"Items:       {len(items)} (en {len(groups)} grupos)")
    print(f"Base URL:    {args.base_url}")
    print(f"Source ID:   {args.source_id}")
    print(f"Output:      {args.output}")
    print(f"Dry run:     {args.dry_run}")
    print()

    if args.dry_run:
        for idx, group in enumerate(groups, 1):
            print(f"[{idx}/{len(groups)}] grupo de {len(group)} turno(s):")
            for item in group:
                print(f"    - {item['id']} [{item['category']}]: {item['question'][:80]}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc)
    errors = 0

    with httpx.Client() as client:
        for group_idx, group in enumerate(groups, 1):
            session_id = str(uuid.uuid4()) if len(group) > 1 else None

            for item in group:
                qid = item["id"]
                question = item["question"]
                t0 = time.monotonic()
                response, err = call_widget_query(
                    client=client,
                    base_url=args.base_url,
                    api_key=args.api_key,
                    source_id=args.source_id,
                    question=question,
                    session_id=session_id,
                    timeout=args.timeout,
                )
                elapsed_ms = (time.monotonic() - t0) * 1000

                if err:
                    errors += 1
                    print(f"  [{qid}] ERROR ({elapsed_ms:.0f}ms): {err}")
                    answer = ""
                    received_session_id = session_id
                    context_chunks: list[str] = []
                else:
                    answer = (response or {}).get("answer", "") if response else ""
                    received_session_id = (response or {}).get("session_id") or session_id
                    context_chunks = list((response or {}).get("context_chunks") or [])
                    if len(group) > 1 and received_session_id:
                        # Para multiturno, usamos el session_id que devolvió el server
                        session_id = received_session_id
                    print(f"  [{qid}] {elapsed_ms:.0f}ms — {answer[:90].replace(chr(10), ' ')}{'…' if len(answer) > 90 else ''}")

                results.append({
                    "id": qid,
                    "question": question,
                    "category": item.get("category"),
                    "difficulty": item.get("difficulty"),
                    "conversation_id": item.get("conversation_id"),
                    "turn_number": item.get("turn_number"),
                    "expected_answer_summary": item.get("expected_answer_summary"),
                    "expected_facts": item.get("expected_facts", []),
                    "ground_truth_urls": item.get("ground_truth_urls", []),
                    "must_not_contain": item.get("must_not_contain", []),
                    "expects_clarification": item.get("expects_clarification", False),
                    "session_id": received_session_id,
                    "answer": answer,
                    "context_chunks": context_chunks,
                    "raw_response": response,
                    "error": err,
                    "elapsed_ms": elapsed_ms,
                })

                if args.sleep > 0:
                    time.sleep(args.sleep)

    finished_at = datetime.now(timezone.utc)
    duration_s = (finished_at - started_at).total_seconds()

    output = {
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": duration_s,
        "base_url": args.base_url,
        "source_id": args.source_id,
        "eval_set_version": dataset.get("version"),
        "site": dataset.get("site"),
        "total_items": len(results),
        "errors": errors,
        "results": results,
    }

    with args.output.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print(f"Listo. {len(results)} resultados, {errors} errores, {duration_s:.1f}s totales.")
    print(f"Guardado en {args.output}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())