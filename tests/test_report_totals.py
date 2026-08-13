"""Garante que o relatório não vende estimativa de busca como total do carrinho."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from orchestrator.report import (
    ItemReport,
    cart_scrape_total,
    estimated_search_total,
    reconcile_with_cart,
    render_report,
)

ROOT = Path(__file__).resolve().parents[1]
SITE_CART_TOTAL = 1077.97
SEARCH_ESTIMATE = 1230.53


def _items_from_run() -> list[ItemReport]:
    data = json.loads((ROOT / "run.json").read_text(encoding="utf-8"))
    out = []
    for it in data["items"]:
        out.append(
            ItemReport(
                raw=it.get("raw") or "",
                status=it.get("status") or "ok",
                chosen_name=it.get("chosen_name"),
                chosen_index=it.get("chosen_index"),
                rule=it.get("rule"),
                qty_target=it.get("qty") or 1,
                packs_added=int(it.get("packs_needed") or 0),
                unit_price=float(it.get("price_num") or 0),
                total_cost=float(it.get("total_cost") or 0),
                notes=list(it.get("notes") or []),
                unit=it.get("unit"),
            )
        )
    return out


class ReportTotalsTest(unittest.TestCase):
    def test_search_estimate_matches_this_run(self):
        items = _items_from_run()
        est = estimated_search_total(items)
        self.assertAlmostEqual(est, SEARCH_ESTIMATE, places=2)

    def test_search_estimate_is_not_the_site_cart(self):
        self.assertGreater(SEARCH_ESTIMATE - SITE_CART_TOTAL, 100)

    def test_header_does_not_claim_estimate_is_cart_when_unscraped(self):
        items = _items_from_run()
        md = render_report(
            "lista-compras.md",
            "perfil-compras.yaml",
            items,
            None,
            None,
            1.0,
            cart_lines_count=0,
            cart_matched=0,
        )
        self.assertIn("não é o total do site", md)
        self.assertNotIn("**Custo estimado total**", md)
        self.assertIn("Estimativa pelos cards da busca", md)

    def test_kg_line_uses_cart_weight_total(self):
        items = [
            ItemReport(
                raw="Carne moída",
                status="ok",
                chosen_name="Carne Bovino Dianteiro Moída Kg",
                chosen_index=0,
                rule="x",
                qty_target=2,
                packs_added=1,
                unit_price=29.99,
                total_cost=29.99,
                notes=[],
                unit="kg",
            )
        ]
        lines = [
            {
                "name": "Carne Bovino Dianteiro Moída Kg",
                "qty": 0.486,
                "price_num": 29.99,
            }
        ]
        out, matched = reconcile_with_cart(items, lines)
        self.assertEqual(matched, 1)
        self.assertAlmostEqual(out[0].cart_line_total, 14.58, places=2)
        self.assertAlmostEqual(cart_scrape_total(out), 14.58, places=2)
        md = render_report("l", "p", out, None, None, 1.0, 1, 1)
        self.assertIn("Total no carrinho (linhas lidas)", md)
        self.assertIn("14.58", md)

    def test_unit_line_multiplies_cart_qty(self):
        items = [
            ItemReport(
                raw="Leite 12",
                status="ok",
                chosen_name="Leite Jussara Zero Lactose 1l",
                chosen_index=0,
                rule="x",
                qty_target=12,
                packs_added=12,
                unit_price=6.99,
                total_cost=83.88,
                notes=[],
                unit="un",
            )
        ]
        lines = [{"name": "Leite Jussara Zero Lactose 1l", "qty": 12, "price_num": 6.99}]
        out, _ = reconcile_with_cart(items, lines)
        self.assertAlmostEqual(out[0].cart_line_total, 83.88, places=2)


if __name__ == "__main__":
    unittest.main()
