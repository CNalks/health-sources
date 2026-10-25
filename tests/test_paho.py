import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from health_sources.adapters import paho
from health_sources.schema import validate

FIX = Path(__file__).parent / "fixtures" / "paho_alerts.html"
CFG = {"id": "paho", "kind": "news", "lang": "en", "url": "https://www.paho.org/en/epidemiological-alerts-and-updates",
       "max_items": 30, "license": "© PAHO/WHO", "figures_allowed": False}
NOW = "2026-10-26T00:00:00+00:00"


class PahoTest(unittest.TestCase):
    def test_parse_list_page(self):
        recs = paho.parse(FIX.read_bytes(), CFG, NOW)
        self.assertGreaterEqual(len(recs), 10)
        for r in recs:
            self.assertEqual(validate(r), [])
            self.assertTrue(r["url"].startswith("https://www.paho.org/en/documents/"))
        first = recs[0]
        self.assertIn("Dengue and Chikungunya", first["title"])
        self.assertEqual(first["published_at"], "2026-10-20T17:35:52+00:00")
        self.assertEqual(first["article_type"], "Epidemiological Update")
        self.assertEqual(len({r["id"] for r in recs}), len(recs))

    def _inbox(self, recs, age_hours=0):
        d = tempfile.mkdtemp()
        p = Path(d) / "paho.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        t = time.time() - age_hours * 3600
        os.utime(p, (t, t))
        return p

    def test_inbox_used_when_present(self):
        recs = paho.parse(FIX.read_bytes(), CFG, NOW)
        p = self._inbox(recs)
        out = paho.fetch({**CFG, "inbox": str(p)}, http=None, fetched_at=NOW)
        self.assertEqual([r["id"] for r in out], [r["id"] for r in recs])

    def test_inbox_too_old_fails(self):
        p = self._inbox(paho.parse(FIX.read_bytes(), CFG, NOW), age_hours=100)
        with self.assertRaises(RuntimeError):
            paho.fetch({**CFG, "inbox": str(p), "inbox_max_age_hours": 72}, http=None, fetched_at=NOW)

    def test_inbox_wrong_source_rejected(self):
        rec = dict(paho.parse(FIX.read_bytes(), CFG, NOW)[0], source="who_don")
        with self.assertRaises(ValueError):
            paho.fetch({**CFG, "inbox": str(self._inbox([rec]))}, http=None, fetched_at=NOW)


if __name__ == "__main__":
    unittest.main()
