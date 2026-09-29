"""Leakage-free replay of dated requests with their historical catalog snapshots.

Input cases need the request, available candidates and the SKU actually bought.
The completed order being predicted is excluded from history via ordered_at.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from .browser import ProductResult
from .fast_plan import _history_decision, _price_basis
from .history import PurchaseHistory


def _lower_wilson(successes: int, attempts: int, z: float = 1.96) -> float:
    if attempts == 0:
        return 0.0
    p = successes / attempts
    denominator = 1 + z * z / attempts
    center = p + z * z / (2 * attempts)
    radius = z * math.sqrt(p * (1 - p) / attempts + z * z / (4 * attempts * attempts))
    return (center - radius) / denominator


def replay(cases: list[dict], history: PurchaseHistory) -> dict:
    correct = automatic = 0
    details = []
    for case in cases:
        requested = str(case["request"]).strip()
        ordered_at = str(case["ordered_at"]).strip()
        expected = str(case["expected_product_id"]).strip()
        if not requested or not ordered_at or not expected:
            raise ValueError("Cada caso precisa de request, ordered_at e expected_product_id")
        candidates = [_price_basis(ProductResult.from_dict({**row, "index": n}))
                      for n, row in enumerate(case["candidates"])]
        item = {"raw": requested, "generic": case.get("generic") or requested,
                "flavor": case.get("flavor"), "preferred_brand": case.get("preferred_brand"),
                "lactose_free": bool(case.get("lactose_free")),
                "qty": case.get("quantity") or 1, "unit": case.get("unit") or "un"}
        evidence = history.related(str(item["generic"]), before=ordered_at)
        decision = _history_decision(item, candidates, evidence)
        chosen = next((r for r in candidates if decision and r.index == decision.index), None)
        predicted = chosen.product_id if chosen else None
        if predicted:
            automatic += 1
            correct += predicted == expected
        details.append({"request": requested, "ordered_at": ordered_at,
                        "predicted_product_id": predicted, "expected_product_id": expected,
                        "correct": predicted == expected if predicted else None})
    evaluated = len(cases)
    precision = correct / automatic if automatic else None
    lower_bound = _lower_wilson(correct, automatic)
    return {"evaluated": evaluated, "automatic": automatic, "correct": correct,
            "precision": precision, "coverage": automatic / evaluated if evaluated else None,
            "precision_95pct_lower_bound": lower_bound,
            "goal_98pct_supported": lower_bound >= 0.98,
            "details": details}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python -m orchestrator.evaluate casos.json")
    cases = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise SystemExit("O arquivo precisa conter uma lista JSON")
    history = PurchaseHistory()
    try:
        print(json.dumps(replay(cases, history), ensure_ascii=False, indent=2))
    finally:
        history.close()


if __name__ == "__main__":
    main()
