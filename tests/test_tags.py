import os
import unittest
from pathlib import Path

# Point at the fixture vocab before importing tags helpers that cache.
FIX = Path(__file__).resolve().parent / "fixtures" / "vocab" / "diseases.json"
os.environ["HS_VOCAB_DISEASES"] = str(FIX)

from health_sources import tags as tagmod  # noqa: E402
from health_sources.adapters.base import make_record  # noqa: E402
from health_sources.schema import validate  # noqa: E402
from tests.helpers import NOW, source_cfg  # noqa: E402


class TagTests(unittest.TestCase):
    def setUp(self):
        tagmod._disease_patterns.cache_clear()
        os.environ["HS_VOCAB_DISEASES"] = str(FIX)

    def test_disease_and_country_from_don_title(self):
        tags = tagmod.disease_tags("Ebola disease caused by Bundibugyo virus - Democratic Republic of the Congo")
        self.assertIn("ebola", tags)
        iso = tagmod.country_iso3("Ebola disease - Democratic Republic of the Congo, Kenya and Nigeria")
        self.assertIn("COD", iso)
        self.assertTrue({"KEN", "NGA"} & set(iso))

    def test_make_record_includes_tags(self):
        cfg = source_cfg("who_don")
        rec = make_record(
            cfg,
            title="Yellow fever - Colombia",
            url="https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON999",
            published_at="2026-10-16T00:00:00+00:00",
            fetched_at=NOW,
        )
        self.assertEqual(validate(rec), [])
        self.assertIn("yellow_fever", rec["disease_tags"])
        self.assertIn("COL", rec["country_iso3"])

    def test_missing_vocab_yields_empty_disease_tags(self):
        tagmod._disease_patterns.cache_clear()
        os.environ["HS_VOCAB_DISEASES"] = "/no/such/vocab.json"
        self.assertEqual(tagmod.disease_tags("Ebola in Kenya"), [])


if __name__ == "__main__":
    unittest.main()
