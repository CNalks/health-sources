import unittest

from health_sources.adapters import europepmc, mmwr, rss, who_don
from health_sources.schema import validate
from tests.helpers import NOW, FakeHttp, fixture, source_cfg


class AdapterTests(unittest.TestCase):
    def assertAllValid(self, records):
        self.assertTrue(records)
        for r in records:
            self.assertEqual(validate(r), [], r)

    def test_mmwr_truncates_and_resolves_links(self):
        cfg = source_cfg("mmwr")
        cfg["max_items"] = 2
        raw = "https://tools.cdc.gov/api/embed/downloader/download.asp?m=342778&c=766718"
        http = FakeHttp({"https://tools.cdc.gov/api/v2": fixture("mmwr.xml")},
                        {raw: "https://www.cdc.gov/mmwr/volumes/75/wr/mm7541a2.htm?s_cid=OS_mm7541a2_x"})
        recs = mmwr.fetch(cfg, http, NOW)
        self.assertEqual(len(recs), 2)
        self.assertAllValid(recs)
        first = recs[0]
        self.assertEqual(first["url"], "https://www.cdc.gov/mmwr/volumes/75/wr/mm7541a2.htm")
        self.assertEqual(first["doi"], "10.15585/mmwr.mm7541a2")
        self.assertIsNone(first["summary"])  # 通用占位描述被丢弃
        self.assertNotIn("MMWR", first["keywords"])  # 通用 category 被忽略，改从标题提取
        self.assertIn("Cosmetic Surgery Procedures", first["keywords"])
        self.assertEqual(first["published_at"], "2026-10-22T17:00:00+00:00")
        self.assertTrue(first["figures_allowed"])

    def test_mmwr_truncate_feed(self):
        out = mmwr.truncate_feed(fixture("mmwr.xml"), 1)
        self.assertEqual(out.count(b"<item>"), 1)
        self.assertTrue(out.rstrip().endswith(b"</rss>"))

    def test_eurosurveillance(self):
        recs = rss.parse(fixture("euro.xml"), source_cfg("eurosurveillance"), NOW)
        self.assertEqual(len(recs), 3)
        self.assertAllValid(recs)
        r = recs[0]
        self.assertTrue(r["doi"].startswith("10.2807/1560-7917.es."))
        self.assertNotIn("TRACK", r["url"])
        self.assertEqual(r["license"], "CC BY 4.0")
        self.assertTrue(r["summary"])
        self.assertTrue(3 <= len(r["keywords"]) <= 5, r["keywords"])

    def test_eid_doi_and_no_author_summary(self):
        recs = rss.parse(fixture("eid.xml"), source_cfg("eid"), NOW)
        self.assertAllValid(recs)
        r = recs[0]
        self.assertRegex(r["doi"], r"^10\.3201/eid\d{4}\.\d{6}$")
        self.assertIsNone(r["summary"])  # "X et al." 不是摘要
        self.assertEqual(r["kind"], "article")

    def test_ecdc_category_keyword(self):
        cfg = source_cfg("ecdc")
        recs = rss.parse(fixture("ecdc_risk.xml"), cfg, NOW, feed_category="Risk assessment")
        self.assertAllValid(recs)
        self.assertEqual(recs[0]["kind"], "news")
        self.assertEqual(recs[0]["keywords"][0], "Risk assessment")
        self.assertIsNone(recs[0]["doi"])

    def test_ecdc_one_subfeed_down(self):
        cfg = source_cfg("ecdc")
        http = FakeHttp({cfg["feeds"][1]["url"]: fixture("ecdc_risk.xml")})
        recs = rss.fetch(cfg, http, NOW)
        self.assertEqual(len(recs), 2)
        self.assertEqual(len(cfg["_warnings"]), 3)

    def test_plos_atom(self):
        recs = rss.parse(fixture("plos.xml"), source_cfg("plos_ntds"), NOW)
        self.assertEqual(len(recs), 3)
        self.assertAllValid(recs)
        r = recs[0]
        self.assertTrue(r["doi"].startswith("10.1371/journal.pntd."))
        self.assertIn("10.1371/journal.pntd", r["url"])  # 斜杠不被转义
        self.assertFalse(r["summary"].startswith("by "))

    def test_who_don(self):
        recs = who_don.parse(fixture("who_don.json"), source_cfg("who_don"), NOW)
        self.assertEqual(len(recs), 3)
        self.assertAllValid(recs)
        r = recs[0]
        self.assertTrue(r["url"].startswith("https://www.who.int/emergencies/disease-outbreak-news/item/"))
        self.assertEqual(r["kind"], "news")
        self.assertFalse(r["figures_allowed"])
        self.assertIn("IGO", r["license"])
        self.assertIn("-", r["title"])  # OverrideTitle 优先

    def test_europepmc_per_record_license(self):
        cfg = source_cfg("europepmc")
        recs = europepmc.parse(fixture("europepmc.json"), cfg, NOW)
        self.assertEqual(len(recs), 3)
        self.assertAllValid(recs)
        self.assertEqual(recs[0]["license"], "CC BY")
        self.assertTrue(recs[0]["figures_allowed"])
        self.assertEqual(recs[0]["keywords"][0], "Outbreak Detection")  # 期刊原有关键词优先
        self.assertLessEqual(len(recs[0]["keywords"]), 6)
        self.assertEqual(recs[1]["license"], "CC BY-NC-ND")
        self.assertFalse(recs[1]["figures_allowed"])
        self.assertTrue(recs[2]["url"].startswith("https://europepmc.org/article/"))
        self.assertEqual(recs[2]["doi"], "10.1371/journal.pone.0357183")
        # 同一篇后来补上 DOI，id 不变
        from health_sources.util import make_id
        self.assertEqual(recs[2]["id"], make_id(None, recs[2]["url"]))
        self.assertNotIn("per_record", [r["license"] for r in recs])

    def test_europepmc_query_window(self):
        from datetime import date
        q = europepmc.build_query({"query": "X", "days": 3}, date(2026, 10, 25))
        self.assertEqual(q, "(X) AND FIRST_PDATE:[2026-10-22 TO 2026-10-25]")


if __name__ == "__main__":
    unittest.main()
