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
    },
}


class PrefsTest(unittest.TestCase):
    def test_qty_paren(self):
        self.assertEqual(extract_qty_from_raw("Sal (2)"), ("Sal", 2))
        self.assertEqual(extract_qty_from_raw("Saco de lixo (lixeira grande)")[1], None)
        self.assertEqual(extract_qty_from_raw("Leite 12"), ("Leite", 12))

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
        self.assertIn("camil", by_raw["Arroz"]["search_term"].lower())
        self.assertEqual(by_raw["Molho de tomate"]["unit"], "un")
        self.assertEqual(by_raw["Luva P"]["unit"], "un")
        self.assertEqual(by_raw["Maçã"]["unit"], "kg")
        cafe = by_raw["Café"]["search_term"].lower()
        self.assertIn("3", by_raw["Café"]["search_term"])
        self.assertIn("orações", cafe)
        self.assertIn("danone", by_raw["Danone"]["search_term"].lower())
        self.assertNotEqual(by_raw["Danone"]["search_term"].lower(), "iogurte")


if __name__ == "__main__":
    unittest.main()
