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
    def test_search_estimate_is_not_the_site_cart(self):
        items = _items_from_run()
        est = estimated_search_total(items)
        self.assertGreater(est, 100)
        self.assertNotAlmostEqual(est, SITE_CART_TOTAL, places=0)

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

    def test_qty_column_does_not_say_12x12(self):
        items = [
            ItemReport(
                raw="Leite (12)",
                status="ok",
                chosen_name="Leite Longa Vida S/Lactose Ninho Levinho Semidesnatado 1l",
                chosen_index=0,
                rule="x",
                qty_target=12,
                packs_added=12,
                unit_price=8.49,
                total_cost=101.88,
                notes=[],
                unit="un",
            )
        ]
        md = render_report("l", "p", items, None, None, 1.0)
        self.assertNotIn("12×12", md)
        self.assertIn("| 12 |", md)

    def test_match_name_does_not_prefer_shorter_without_lactose(self):
        from orchestrator.browser import ProductResult
        from orchestrator.main import _match_name

        def pr(i, name):
            return ProductResult.from_dict(
                {
                    "index": i,
                    "name": name,
                    "price_num": 1,
                    "price_per_base_unit": 1,
                    "price_base_dim": "un",
                    "sale_unit": "UN",
                }
            )

        regular = pr(0, "Creme De Leite Piracanjuba Cx 200g")
        sl = pr(7, "Creme De Leite S/Lactose Piracanjuba Cx 200g")
        hit = _match_name([regular, sl], "Creme De Leite S/Lactose Piracanjuba Cx 200g")
        self.assertIsNotNone(hit)
        self.assertIn("Lactose", hit.name)

    def test_requeijao_does_not_match_other_brand(self):
        from orchestrator.report import _names_match

        self.assertFalse(
            _names_match(
                "Requeijão Polenghi Zero Lactose 200G",
                "Requeijão Cremoso Danone Tradicional 200G",
            )
        )
        self.assertTrue(
            _names_match(
                "Requeijão Polenghi Zero Lactose 200G",
                "Requeijão Polenghi Zero Lactose 200G",
            )
        )

    def test_drawer_line_total_not_multiplied_again(self):
        items = [
            ItemReport(
                raw="Leite (12)",
                status="ok",
                chosen_name="Leite Longa Vida S/Lactose Ninho Levinho Semidesnatado 1l",
                chosen_index=0,
                rule="x",
                qty_target=12,
                packs_added=12,
                unit_price=8.49,
                total_cost=101.88,
                notes=[],
                unit="un",
            )
        ]
        lines = [
            {
                "name": "Leite Longa Vida S/Lactose Ninho Levinho Semidesnatado 1l",
                "qty": 12,
                "price_num": 101.88,
            }
        ]
        out, _ = reconcile_with_cart(items, lines)
        self.assertAlmostEqual(out[0].cart_line_total, 101.88, places=2)

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
