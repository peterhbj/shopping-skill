import unittest
from dataclasses import asdict

from orchestrator.enricher import (
    apply_catalog_mapping,
    catalog_candidates,
    enrich_item,
)


class JevEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "sem_lactose": True,
            "apelidos": {"leite especial": "leite"},
            "marcas": {"leite": "Marca X"},
            "quantidades": {"leite": 12, "cenoura": 2},
        }

    def test_shortlist_is_bounded_and_keeps_current_interpretation(self):
        item = {"raw": "Leite novo", "item_text": "Leite novo", "generic": "Leite novo"}
        choices = catalog_candidates(item, self.profile, limit=4)
        self.assertLessEqual(len(choices), 4)
        self.assertEqual(choices[0], "Leite novo")

    def test_mapping_preserves_explicit_quantity_and_flavor(self):
        item = asdict(enrich_item("Leite especial (8)", self.profile))
        item["raw"] = "Leite especial (8) (morango)"
        item["flavor"] = "morango"
        mapped = apply_catalog_mapping(item, "leite", self.profile, 0.96)
        self.assertEqual(mapped["qty"], 8)
        self.assertEqual(mapped["preferred_brand"], "Marca X")
        self.assertIn("morango", mapped["search_term"])
        self.assertTrue(mapped["lactose_free"])
        self.assertEqual(mapped["match_score"], 0.96)


if __name__ == "__main__":
    unittest.main()
