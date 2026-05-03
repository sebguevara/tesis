#!/usr/bin/env python3
"""
Scorer del eval set: lee un JSON producido por run_eval.py y calcula
correctness, faithfulness, hallucination y refusal_correct usando
gpt-4o-mini como LLM-as-judge.

Uso:
    python score_eval.py \\
        --results results/00_baseline.json \\
        --output results/scored_00_baseline.json \\
        --judge-model gpt-4o-mini

Variables de entorno:
    OPENAI_API_KEY   requerido
    JUDGE_MODEL      opcional (default gpt-4o-mini)

Métricas por ítem:
    correctness    [0..1] — qué tanto cubre los expected_facts
    faithfulness   [0..1] — qué tanto cada afirmación está soportada por context_chunks
    hallucination  bool   — true si menciona algo de must_not_contain o inventa
    refusal_correct bool|null — para categorías ambiguous/out_of_scope: true si
                                el sistema correctamente rechazó/pidió aclaración
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI


JUDGE_SYSTEM = (
    "Sos un evaluador estricto de respuestas de un asistente RAG sobre el sitio "
    "de la Facultad de Medicina de la UNNE. Devolvés SIEMPRE JSON válido, "
    "sin texto fuera del JSON, sin markdown, sin comentarios. Sin emojis."
)


CORRECTNESS_PROMPT = """\
Evaluá si la RESPUESTA cubre los HECHOS_ESPERADOS.
Devolvé JSON: {{"score": <float entre 0 y 1>, "rationale": "<máx 30 palabras>"}}

Reglas:
- 1.0 = todos los hechos esperados están presentes y son correctos.
  - Aceptá paráfrasis: "1280 + 320 horas" cubre "1600 horas" (la suma vale).
  - Aceptá variantes ortográficas: "Anatomía" cubre "anatomia".
  - Aceptá la info aunque la respuesta agregue datos correctos adicionales.
- 0.5 = parcial: cubre la mitad de los hechos, o falta precisión en alguno.
- 0.0 = no cubre ninguno de los hechos esperados, o dice "no sé" cuando los hechos sí existen.
- Si la respuesta es un decline ("no encontré evidencia suficiente") pero la pregunta
  era de opinión / out-of-scope (HECHOS_ESPERADOS vacío o pregunta tipo "el mejor X"),
  score = 1.0 (declinar es lo correcto).

PREGUNTA: {question}
HECHOS_ESPERADOS: {expected_facts}
RESUMEN_ESPERADO: {expected_summary}
RESPUESTA_DEL_SISTEMA: {answer}
"""


FAITHFULNESS_PROMPT = """\
Evaluá si cada afirmación factual de la RESPUESTA está soportada por el CONTEXTO.
Devolvé JSON: {{"score": <float entre 0 y 1>, "unsupported_claims": ["<afirmación 1>", ...]}}

Reglas:
- 1.0 = todas las afirmaciones factuales están en el contexto.
- 0.5 = aprox. la mitad están soportadas.
- 0.0 = ninguna o casi ninguna está soportada.
- Las frases de cortesía / saludos / "no encontré" no cuentan como afirmaciones.
- Si la respuesta es "no sé" o pide aclaración → score = 1.0 (no afirma nada falso).
- Si no hay CONTEXTO y la respuesta hace afirmaciones factuales concretas → score = 0.0.

CONTEXTO_RECUPERADO:
{context}

RESPUESTA_DEL_SISTEMA:
{answer}
"""


HALLUCINATION_PROMPT = """\
Detectá si la RESPUESTA contiene alucinaciones (criterio estricto).

IMPORTANTE: "Alucinación" significa AFIRMAR ALGO FALSO O INVENTADO. NO significa
"respuesta incompleta". Si la respuesta omite algunos HECHOS_ESPERADOS pero lo que
SÍ dice es correcto, eso es un problema de COBERTURA (correctness baja), NO una
alucinación. Solo marcá hallucinated=true si la respuesta dice algo FALSO o
INVENTADO (no por omitir).

Devolvé JSON: {{"hallucinated": <true|false>, "reason": "<máx 25 palabras>"}}

ALUCINA si (basta UNA, todas son sobre afirmar algo falso/inventado):
- Menciona literal o equivalente cualquier elemento de PROHIBIDOS_LITERALES.
- CONTRADICE los HECHOS_ESPERADOS (afirma algo opuesto, distinto o incompatible
  con lo esperado — no solo "no menciona todo").
- Cita una URL, email, nombre propio, fecha, número, cargo o entidad que NO
  aparece en el CONTEXTO ni en HECHOS_ESPERADOS y que es claramente una
  afirmación factual nueva (inventada).

NO alucina si:
- Dice "no sé", "no encontré", o pide aclaración (decline correcto).
- Es INCOMPLETA — menciona algunos hechos esperados pero no todos. Esto baja
  correctness, no es hallucination. ¡No marques hallucinated=true por esto!
- Solo menciona hechos del CONTEXTO o HECHOS_ESPERADOS, aunque omita otros.
- Agrega información correcta extra que SÍ está en el CONTEXTO (ej: "Bioquímica
  tiene 120 horas en Medicina y 60 en Enfermería" cuando la pregunta es por
  Bioquímica sin especificar carrera y ambos datos están en el contexto).
- Hace una operación matemática trivial sobre datos del CONTEXTO ("1280+320=1600"
  cubre "1600 horas").
- Usa tiempos verbales distintos a los esperados (ej: "fue" vs "es") cuando el dato
  es correcto. Eso es estilo, no alucinación.
- Usa una variante ortográfica/normalizada del mismo dato (ej: "Anatomía Humana
  Normal" cuando HECHOS_ESPERADOS dice "Anatomía").
- Cita una entidad real del sitio que no figura en HECHOS_ESPERADOS pero sí está
  presente en el CONTEXTO (ej: nombrar una secretaría adicional que existe).

EJEMPLOS para fijar el criterio:

Pregunta: "¿Qué materias se cursan en el primer año de Medicina?"
HECHOS_ESPERADOS: ["Medicina, Hombre y Sociedad", "Anatomía", "Bioquímica", "Histología"]
Respuesta: "En el primer año se cursa Medicina, Hombre y Sociedad (400 horas)."
→ {{"hallucinated": false, "reason": "Respuesta incompleta (cobertura baja), no inventa nada."}}
Esto es CORRECTNESS bajo, NO hallucination.

Pregunta: "¿Cuántas horas tiene la materia Bioquímica?"
HECHOS_ESPERADOS: ["120 horas"]
Respuesta: "Bioquímica tiene 120 horas en Medicina y 60 en Enfermería."
→ {{"hallucinated": false, "reason": "Info extra (60 hs Enfermería) está en el contexto."}}

Pregunta: "¿La carrera de Medicina es presencial o virtual?"
HECHOS_ESPERADOS: ["presencial"]
PROHIBIDOS: ["virtual", "semipresencial"]
Respuesta: "Combina presencial y virtual."
→ {{"hallucinated": true, "reason": "Menciona 'virtual', está en PROHIBIDOS."}}

Pregunta: "¿Cuál es el mejor libro para Anatomía?"
Respuesta: "No puedo recomendar un libro específico como 'el mejor'."
→ {{"hallucinated": false, "reason": "Decline correcto a pregunta de opinión."}}

PREGUNTA: {question}
HECHOS_ESPERADOS: {expected_facts}
PROHIBIDOS_LITERALES: {must_not_contain}
CONTEXTO_RECUPERADO:
{context}
RESPUESTA_DEL_SISTEMA: {answer}
"""


REFUSAL_PROMPT = """\
Esta pregunta requiere que el sistema NO responda con datos concretos
(porque es ambigua o está fuera del alcance del sitio).
Decidí si la RESPUESTA correctamente pide aclaración o admite no tener info.
Devolvé JSON: {{"refusal_correct": <true|false>, "reason": "<máx 25 palabras>"}}

- true si la respuesta pide más contexto, aclara que necesita más datos, o dice que no encontró información.
- false si la respuesta da una afirmación factual concreta como si fuese cierta.

PREGUNTA: {question}
CATEGORIA: {category}
RESPUESTA_DEL_SISTEMA: {answer}
"""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--results", required=True, type=Path, help="JSON producido por run_eval.py")
    p.add_argument("--output", required=True, type=Path, help="Path al archivo de salida JSON con scores")
    p.add_argument(
        "--judge-model",
        # Stage 5: gpt-4o-mini deja muchos falsos positivos en hallucination
        # (suma matemática, info extra correcta, tiempos verbales). gpt-4o es
        # más caro pero notablemente más estricto y reduce el ruido.
        default=os.environ.get("JUDGE_MODEL", "gpt-4o"),
        help="Modelo OpenAI a usar como juez",
    )
    p.add_argument("--limit", type=int, default=0, help="Limitar a N ítems (0 = todos)")
    p.add_argument("--sleep", type=float, default=0.1, help="Pausa entre llamadas al juez")
    return p.parse_args()


def _truncate_context(chunks: list[str], max_chars: int = 12000) -> str:
    if not chunks:
        return "(sin contexto recuperado)"
    joined = "\n\n---\n\n".join(chunks)
    if len(joined) <= max_chars:
        return joined
    return joined[:max_chars] + "\n[…truncado…]"


def _judge_json_call(client: OpenAI, model: str, system: str, prompt: str) -> dict[str, Any]:
    """Single call expecting a JSON object response."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_parse_error": raw[:300]}


def _score_item(client: OpenAI, model: str, item: dict[str, Any]) -> dict[str, Any]:
    answer = (item.get("answer") or "").strip()
    question = (item.get("question") or "").strip()
    expected_facts = item.get("expected_facts") or []
    expected_summary = item.get("expected_answer_summary") or ""
    must_not_contain = item.get("must_not_contain") or []
    category = (item.get("category") or "").strip().lower()
    context_chunks = item.get("context_chunks") or []
    expects_clarification = bool(item.get("expects_clarification"))

    refusal_categories = {"ambiguous", "out_of_scope"}
    is_refusal_case = category in refusal_categories or expects_clarification

    scored: dict[str, Any] = {
        "id": item.get("id"),
        "category": category,
        "difficulty": item.get("difficulty"),
        "expects_clarification": expects_clarification,
        "answer": answer,
        "error": item.get("error"),
        "elapsed_ms": item.get("elapsed_ms"),
        "context_chunks_count": len(context_chunks),
    }

    if not answer:
        scored.update({
            "correctness": 0.0,
            "faithfulness": 0.0,
            "hallucinated": False,
            "refusal_correct": None,
            "judge_error": "empty_answer",
        })
        return scored

    if is_refusal_case:
        # For refusal cases we only judge refusal correctness and hallucination.
        refusal_raw = _judge_json_call(
            client,
            model,
            JUDGE_SYSTEM,
            REFUSAL_PROMPT.format(question=question, category=category, answer=answer),
        )
        halluc_raw = _judge_json_call(
            client,
            model,
            JUDGE_SYSTEM,
            HALLUCINATION_PROMPT.format(
                question=question,
                expected_facts=json.dumps(expected_facts, ensure_ascii=False),
                must_not_contain=json.dumps(must_not_contain, ensure_ascii=False),
                context=_truncate_context(context_chunks),
                answer=answer,
            ),
        )
        scored.update({
            "correctness": None,
            "faithfulness": None,
            "hallucinated": bool(halluc_raw.get("hallucinated", False)),
            "hallucination_reason": halluc_raw.get("reason"),
            "refusal_correct": bool(refusal_raw.get("refusal_correct", False)),
            "refusal_reason": refusal_raw.get("reason"),
        })
        return scored

    correctness_raw = _judge_json_call(
        client,
        model,
        JUDGE_SYSTEM,
        CORRECTNESS_PROMPT.format(
            question=question,
            expected_facts=json.dumps(expected_facts, ensure_ascii=False),
            expected_summary=expected_summary,
            answer=answer,
        ),
    )
    faithfulness_raw = _judge_json_call(
        client,
        model,
        JUDGE_SYSTEM,
        FAITHFULNESS_PROMPT.format(
            context=_truncate_context(context_chunks),
            answer=answer,
        ),
    )
    halluc_raw = _judge_json_call(
        client,
        model,
        JUDGE_SYSTEM,
        HALLUCINATION_PROMPT.format(
            question=question,
            expected_facts=json.dumps(expected_facts, ensure_ascii=False),
            must_not_contain=json.dumps(must_not_contain, ensure_ascii=False),
            context=_truncate_context(context_chunks),
            answer=answer,
        ),
    )

    correctness = float(correctness_raw.get("score") or 0.0)
    faithfulness = float(faithfulness_raw.get("score") or 0.0)
    scored.update({
        "correctness": max(0.0, min(1.0, correctness)),
        "correctness_rationale": correctness_raw.get("rationale"),
        "faithfulness": max(0.0, min(1.0, faithfulness)),
        "faithfulness_unsupported": faithfulness_raw.get("unsupported_claims") or [],
        "hallucinated": bool(halluc_raw.get("hallucinated", False)),
        "hallucination_reason": halluc_raw.get("reason"),
        "refusal_correct": None,
    })
    return scored


def _aggregate(scored_items: list[dict[str, Any]]) -> dict[str, Any]:
    def _avg(values: list[float]) -> float | None:
        clean = [v for v in values if v is not None]
        return round(sum(clean) / len(clean), 3) if clean else None

    def _rate(values: list[bool]) -> float | None:
        return round(sum(1 for v in values if v) / len(values), 3) if values else None

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for it in scored_items:
        by_cat[(it.get("category") or "unknown")].append(it)

    def _agg_block(items: list[dict]) -> dict[str, Any]:
        return {
            "n": len(items),
            "correctness_avg": _avg([float(x["correctness"]) for x in items if x.get("correctness") is not None]),
            "faithfulness_avg": _avg([float(x["faithfulness"]) for x in items if x.get("faithfulness") is not None]),
            "hallucination_rate": _rate([bool(x.get("hallucinated")) for x in items]),
            "refusal_correct_rate": _rate(
                [bool(x.get("refusal_correct")) for x in items if x.get("refusal_correct") is not None]
            ),
            "errors": sum(1 for x in items if x.get("error")),
        }

    return {
        "global": _agg_block(scored_items),
        "by_category": {cat: _agg_block(items) for cat, items in sorted(by_cat.items())},
    }


def main() -> int:
    args = _parse_args()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("ERROR: env OPENAI_API_KEY requerido", file=sys.stderr)
        return 2
    if not args.results.exists():
        print(f"ERROR: no existe {args.results}", file=sys.stderr)
        return 2

    with args.results.open(encoding="utf-8") as f:
        run_data = json.load(f)
    items = run_data.get("results") or []
    if args.limit > 0:
        items = items[: args.limit]

    print(f"Scoring {len(items)} ítems con {args.judge_model}…")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    client = OpenAI(api_key=api_key)
    scored: list[dict[str, Any]] = []
    started_at = datetime.now(timezone.utc)

    for idx, item in enumerate(items, 1):
        try:
            scored_item = _score_item(client, args.judge_model, item)
        except Exception as exc:
            scored_item = {
                "id": item.get("id"),
                "category": item.get("category"),
                "answer": item.get("answer"),
                "judge_error": f"exception: {exc}",
                "correctness": None,
                "faithfulness": None,
                "hallucinated": False,
                "refusal_correct": None,
            }
        scored.append(scored_item)
        flag = "H" if scored_item.get("hallucinated") else "·"
        c = scored_item.get("correctness")
        f = scored_item.get("faithfulness")
        c_str = f"{c:.2f}" if isinstance(c, (int, float)) else "—"
        f_str = f"{f:.2f}" if isinstance(f, (int, float)) else "—"
        print(f"  [{idx}/{len(items)}] {scored_item.get('id')} c={c_str} f={f_str} {flag}")
        if args.sleep > 0:
            time.sleep(args.sleep)

    finished_at = datetime.now(timezone.utc)
    aggregates = _aggregate(scored)

    output = {
        "scored_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 2),
        "judge_model": args.judge_model,
        "source_run": str(args.results),
        "site": run_data.get("site"),
        "eval_set_version": run_data.get("eval_set_version"),
        "aggregates": aggregates,
        "items": scored,
    }
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print()
    print("=== Resumen global ===")
    g = aggregates["global"]
    print(f"  n={g['n']}  errors={g['errors']}")
    print(f"  correctness_avg={g['correctness_avg']}")
    print(f"  faithfulness_avg={g['faithfulness_avg']}")
    print(f"  hallucination_rate={g['hallucination_rate']}")
    print(f"  refusal_correct_rate={g['refusal_correct_rate']}")
    print()
    print("=== Por categoría ===")
    for cat, block in aggregates["by_category"].items():
        print(
            f"  {cat:<22} n={block['n']:>2}  c={block['correctness_avg']}  "
            f"f={block['faithfulness_avg']}  hall={block['hallucination_rate']}  "
            f"refusal={block['refusal_correct_rate']}"
        )
    print()
    print(f"Guardado en {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
