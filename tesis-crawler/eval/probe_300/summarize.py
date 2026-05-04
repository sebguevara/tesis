"""
Summarize results.jsonl produced by run.py.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = HERE / "results.jsonl"
SUMMARY = HERE / "summary.json"
FAILURES = HERE / "failures.json"


def main():
    rows = [json.loads(l) for l in RESULTS.read_text(encoding="utf-8").splitlines() if l.strip()]
    total = len(rows)
    ok = sum(1 for r in rows if r.get("ok"))
    fail = total - ok

    by_cat: dict[str, dict] = defaultdict(lambda: {"total": 0, "ok": 0, "fail": 0})
    for r in rows:
        c = r.get("category", "unknown")
        by_cat[c]["total"] += 1
        if r.get("ok"):
            by_cat[c]["ok"] += 1
        else:
            by_cat[c]["fail"] += 1

    cats_sorted = sorted(
        by_cat.items(),
        key=lambda kv: (kv[1]["fail"] / max(1, kv[1]["total"]), -kv[1]["total"]),
        reverse=True,
    )

    avg_duration = sum(r.get("duration_s", 0) for r in rows) / max(1, total)
    grounds = [r["groundedness"] for r in rows if isinstance(r.get("groundedness"), (int, float))]
    avg_ground = sum(grounds) / max(1, len(grounds))

    summary = {
        "total": total,
        "ok": ok,
        "fail": fail,
        "ok_pct": round(100 * ok / max(1, total), 2),
        "avg_duration_s": round(avg_duration, 2),
        "avg_groundedness": round(avg_ground, 3),
        "by_category": {
            c: {
                **stats,
                "ok_pct": round(100 * stats["ok"] / max(1, stats["total"]), 1),
            }
            for c, stats in by_cat.items()
        },
    }
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    failures = [
        {
            "id": r["id"],
            "category": r["category"],
            "q": r["q"],
            "answer": r.get("answer", "")[:300],
            "groundedness": r.get("groundedness"),
            "expects_decline": r.get("expects_decline"),
            "decline": r.get("decline"),
            "error": r.get("error"),
        }
        for r in rows
        if not r.get("ok")
    ]
    FAILURES.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {len(failures)} failures to {FAILURES.name}")
    print("\n=== Top categories by failure rate ===")
    for c, s in cats_sorted[:15]:
        print(f"  {c:<28} {s['fail']}/{s['total']} fails ({100*s['fail']/max(1,s['total']):.0f}%)")


if __name__ == "__main__":
    main()
