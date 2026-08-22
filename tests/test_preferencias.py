"""Preferências curtas + lista com checkbox e qty (2)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.enricher import enrich_item, enrich_list, extract_qty_from_raw, parse_list_file


PREFS = {
    "sem_lactose": True,
    "apelidos": {
        "oleo normal": "óleo de soja",
        "bife": "patinho",
    },
    "marcas": {
        "arroz": "Camil",
        "leite": "Ninho Levinho",
        "creme de leite": "Italac",
    },
    "quantidades": {
        "creme de leite": 6,
        "bife": 1.8,
        "leite": 12,
    },
}


class PrefsTest(unittest.TestCase):
    def test_qty_paren(self):
        self.assertEqual(extract_qty_from_raw("Sal (2)"), ("Sal", 2))
        self.assertEqual(extract_qty_from_raw("Saco de lixo (lixeira grande)")[1], None)
        self.assertEqual(extract_qty_from_raw("Leite 12"), ("Leite", 12))
        self.assertEqual(extract_qty_from_raw("Tomate (1Kg)"), ("Tomate", 1))
        self.assertEqual(extract_qty_from_raw("Tomate (1 kg)"), ("Tomate", 1))
        self.assertEqual(extract_qty_from_raw("Chimichurri (200g)"), ("Chimichurri", 0.2))
        self.assertEqual(extract_qty_from_raw("Banana 2kg"), ("Banana", 2))

    def test_checkbox_and_qty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "lista.md"
            p.write_text(
                "# Lista\n\n- [ ] Arroz\n- [ ] Sal (2)\n- [x] Óleo normal\n",
                encoding="utf-8",
            )
            items = parse_list_file(p)
            texts = [t for t, *_ in items]
            self.assertIn("Arroz", texts)
            self.assertIn("Sal (2)", texts)
            self.assertIn("Óleo normal", texts)

    def test_alias_and_brand(self):
        oleo = enrich_item("Óleo normal", PREFS)
        self.assertEqual(oleo.generic, "óleo de soja")
        self.assertIn("óleo", oleo.search_term.lower())
        self.assertTrue(oleo.rotation)

        arroz = enrich_item("Arroz", PREFS)
        self.assertEqual(arroz.preferred_brand, "Camil")
        self.assertIn("camil", arroz.search_term.lower())
        self.assertFalse(arroz.rotation)
        self.assertEqual(arroz.qty, 1)

        sal = enrich_item("Sal (2)", PREFS)
        self.assertEqual(sal.qty, 2)

        creme = enrich_item("Creme de leite", PREFS)
        self.assertEqual(creme.qty, 6)
        self.assertEqual(creme.preferred_brand, "Italac")
        self.assertTrue(creme.lactose_free)

        bife = enrich_item("Bife", PREFS)
        self.assertEqual(bife.qty, 1.8)
        self.assertEqual(bife.generic, "patinho")

        leite12 = enrich_item("Leite (6)", PREFS)
        self.assertEqual(leite12.qty, 6)

    def test_leite_marca_sl(self):
        leite = enrich_item("Leite", PREFS)
        self.assertTrue(leite.lactose_free)
        self.assertEqual(leite.preferred_brand, "Ninho Levinho")

    def test_enrich_real_list_if_present(self):
        lista = Path("lista-compras.md")
        prefs = Path("preferencias.yaml")
        if not lista.is_file() or not prefs.is_file():
            self.skipTest("arquivos do repo")
        result = enrich_list(lista, prefs)
        self.assertGreater(result["meta"]["total_items"], 10)
        by_raw = {i["item_text"]: i for i in result["items"]}
        self.assertEqual(by_raw["Sal"]["qty"], 2)
        self.assertEqual(by_raw["Creme de leite"]["qty"], 6)
        self.assertEqual(by_raw["Água"]["qty"], 12)
        self.assertIn("gás", by_raw["Água"]["generic"].lower() + by_raw["Água"]["search_term"].lower())
        self.assertIn("camil", by_raw["Arroz"]["search_term"].lower())
        self.assertEqual(by_raw["Molho de tomate"]["unit"], "un")
        self.assertEqual(by_raw["Luva P"]["unit"], "un")
        self.assertEqual(by_raw["Maçã"]["unit"], "kg")
        self.assertEqual(by_raw["Tomate"]["qty"], 1)
        self.assertEqual(by_raw["Tomate"]["unit"], "kg")
        palha = next(i for i in result["items"] if "palha" in (i.get("item_text") or "").lower())
        self.assertEqual(palha["unit"], "un")
        self.assertIn("yoki", palha["search_term"].lower())
        saco_p = next(
            i for i in result["items"]
            if "pequena" in (i.get("raw") or "").lower()
        )
        self.assertIn("15", saco_p["generic"].lower() + saco_p["search_term"].lower())
        self.assertEqual(saco_p["qty"], 5)
        cafe = by_raw["Café"]["search_term"].lower()
        self.assertIn("3", by_raw["Café"]["search_term"])
        self.assertIn("orações", cafe)
        danone = next(i for i in result["items"] if "danone" in (i.get("item_text") or "").lower())
        self.assertIn("danone", danone["search_term"].lower())
        self.assertNotEqual(danone["search_term"].lower(), "iogurte")


if __name__ == "__main__":
    unittest.main()
