#!/usr/bin/env python3
"""
Genera un eval set automático para cualquier sitio ya indexado.

Toma documentos al azar del corpus de un dominio, le pide a gpt-4o que escriba
la pregunta que haría un estudiante cuya respuesta esté COMPLETA en ese
fragmento, y guarda el resultado en el mismo formato que eval_set.json, así
run_eval.py y score_eval.py funcionan sin cambios. Suma algunas preguntas fijas
fuera de alcance para medir que el sistema sepa decir que no.

    uv run python eval/gen_eval_set.py --domain arq.unne.edu.ar --n 60 \
        --output eval/sets/arq.json

shortcut: las preguntas salen del corpus indexado, así que miden si el sistema
encuentra lo que ya tiene; lo que el crawler no indexó lo mide sitemap_coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date
from pathlib import Path

import psycopg
from openai import OpenAI

CATEGORIES = ("factual_simple", "listing", "requirements", "dates", "contact", "authority")

GEN_SYSTEM = (
    "Escribís preguntas de evaluación para un asistente de un sitio universitario. "
    "Devolvés SIEMPRE un JSON válido, sin markdown."
)

GEN_PROMPT = """\
Sitio: {domain}
Página: {title} ({url})

FRAGMENTO:
\"\"\"
{chunk}
\"\"\"

Si el fragmento tiene un dato institucional útil y estable (carreras, materias,
requisitos, trámites, fechas, contactos, autoridades, horarios, aranceles, sedes),
escribí UNA pregunta que haría un estudiante o aspirante y cuya respuesta esté
COMPLETA en el fragmento.
- Escribila como la escribiría una persona, sin copiar frases del fragmento.
- Tiene que entenderse sola: nombrá la carrera, área o trámite del que habla.
- No preguntes por detalles triviales de una noticia (quién sacó una foto, etc.).

Devolvé {{"usable": false}} si el fragmento es menú, pie de página, listado de
links o algo sin dato útil, y también si es gestión interna que un estudiante,
aspirante o docente no consultaría: compras, licitaciones, órdenes de compra,
datos de proveedores, dictámenes o designaciones de una persona puntual,
padrones, actas, contenido de artículos científicos.

JSON:
{{"usable": true,
  "question": "...",
  "expected_answer_summary": "respuesta correcta en 1 oración",
  "expected_facts": ["1 a 3 datos cortos y literales que la respuesta debe contener"],
  "category": "una de: {categories}"}}
"""

SPECIFIC_PROMPT = """\
Un usuario le escribe esta pregunta al asistente del sitio {domain}, sin ver
ninguna página:

"{question}"

¿La pregunta identifica sin ambigüedad de qué tema, trámite, carrera, curso,
evento o documento habla, de modo que tenga UNA respuesta correcta en el sitio?
Es ambigua si se refiere a "el documento", "la resolución", "el proyecto", "el
concurso" sin decir cuál, o si podría aplicar a muchas páginas distintas.

JSON: {{"specific": true|false}}
"""

OUT_OF_SCOPE = [
    "¿Cuál es el mejor libro para estudiar en primer año?",
    "¿Qué opinás de la situación política del país?",
    "¿Esta facultad es mejor que la de otra universidad?",
]


def _db_url() -> str:
    url = os.environ.get("DB_URL", "")
    if not url:
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("DB_URL="):
                url = line.split("=", 1)[1].strip().strip('"')
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--n", type=int, default=60, help="preguntas respondibles a generar")
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    domain = args.domain.lower().removeprefix("www.")
    with psycopg.connect(_db_url()) as conn:
        rows = conn.execute(
            """
            SELECT d.canonical_url, COALESCE(d.title, ''), c.text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            JOIN sources s ON s.source_id = d.source_id
            WHERE lower(s.domain) IN (%s, %s) AND length(c.text) > 200
            """,
            (domain, "www." + domain),
        ).fetchall()
    if not rows:
        print(f"ERROR: no hay chunks para {domain}", file=sys.stderr)
        return 2

    # One chunk per document so a few huge pages don't dominate the set.
    by_url: dict[str, list[tuple[str, str, str]]] = {}
    for r in rows:
        by_url.setdefault(r[0], []).append(r)
    rng = random.Random(args.seed)
    # Students mostly read pages, not attached PDFs: interleave 2 HTML : 1 PDF.
    html = [u for u in by_url if not u.lower().endswith(".pdf")]
    pdfs = [u for u in by_url if u.lower().endswith(".pdf")]
    rng.shuffle(html)
    rng.shuffle(pdfs)
    urls: list[str] = []
    while html or pdfs:
        urls += html[:2] + pdfs[:1]
        html, pdfs = html[2:], pdfs[1:]

    client = OpenAI()
    items: list[dict] = []
    for url in urls:
        if len(items) >= args.n:
            break
        _, title, chunk = rng.choice(by_url[url])
        resp = client.chat.completions.create(
            model=args.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": GEN_SYSTEM},
                {"role": "user", "content": GEN_PROMPT.format(
                    domain=domain, title=title, url=url, chunk=chunk[:2500],
                    categories=", ".join(CATEGORIES),
                )},
            ],
        )
        try:
            g = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            continue
        if not g.get("usable") or not g.get("question") or not g.get("expected_facts"):
            continue
        # Self-containedness check without the fragment: "¿quién autorizó el
        # documento?" is unanswerable for a real user when the site has hundreds
        # of such documents, so it measures the generator, not the assistant.
        chk = client.chat.completions.create(
            model=args.model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": SPECIFIC_PROMPT.format(domain=domain, question=g["question"])}],
        )
        try:
            if not json.loads(chk.choices[0].message.content or "{}").get("specific"):
                print(f"  (descartada por ambigua) {g['question']}")
                continue
        except json.JSONDecodeError:
            continue
        items.append({
            "id": f"G{len(items) + 1:03d}",
            "category": g.get("category") if g.get("category") in CATEGORIES else "factual_simple",
            "difficulty": "auto",
            "question": g["question"],
            "expected_answer_summary": g.get("expected_answer_summary", ""),
            "expected_facts": g["expected_facts"][:3],
            "ground_truth_urls": [url],
            "must_not_contain": [],
            "notes": "auto-generado desde el corpus",
        })
        print(f"  {items[-1]['id']} [{items[-1]['category']}] {g['question']}")

    for q in OUT_OF_SCOPE:
        items.append({
            "id": f"O{len(items) + 1:03d}",
            "category": "out_of_scope",
            "difficulty": "auto",
            "question": q,
            "expected_answer_summary": "Declina: no es una consulta institucional.",
            "expected_facts": [],
            "ground_truth_urls": [],
            "must_not_contain": [],
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "version": "auto-1",
        "site": domain,
        "created_at": date.today().isoformat(),
        "description": f"Eval set auto-generado desde el corpus de {domain}.",
        "items": items,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(items)} ítems → {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
