import json
import tempfile
import unittest
from pathlib import Path

from health_sources import keywords, runner, util
from health_sources.schema import validate
from health_sources.store import Store
from tests.helpers import NOW, ROOT, FakeHttp, fixture


def good_record(**over):
    rec = {
        "id": "0123456789abcdef", "source": "x", "kind": "article", "title": "T", "lang": "en",
        "published_at": "2026-10-22T00:00:00+00:00", "url": "https://e.org/a", "doi": None,
        "summary": None, "keywords": ["a"], "license": None, "figures_allowed": False,
        "fetched_at": NOW, "status": "ok",
    }
    rec.update(over)
    return rec


class SchemaTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate(good_record()), [])

    def test_errors(self):
        self.assertTrue(validate(good_record(kind="data")))
        self.assertTrue(validate(good_record(published_at="2026-10-22")))  # 无时区
        self.assertTrue(validate(good_record(doi="abc")))
        self.assertTrue(validate(good_record(figures_allowed="yes")))
        self.assertTrue(validate(good_record(status="new")))
        r = good_record(); r.pop("title")
        self.assertTrue(validate(r))
        self.assertTrue(validate(good_record(extra=1)))


class UtilTests(unittest.TestCase):
    def test_normalize_url(self):
        self.assertEqual(util.normalize_url("HTTPS://Example.org/a/?utm_source=x&TRACK=RSS&b=2#f"),
                         "https://example.org/a?b=2")

    def test_id_prefers_doi(self):
        a = util.make_id("10.1/ABC", "https://a.org/x")
        b = util.make_id("10.1/abc", "https://b.org/y")
        self.assertEqual(a, b)
        self.assertNotEqual(util.make_id(None, "https://a.org/x"), a)
        self.assertEqual(util.make_id(None, "https://a.org/x?utm_medium=1"), util.make_id(None, "https://a.org/x"))

    def test_doi_from_url(self):
        self.assertEqual(util.doi_from_url("https://wwwnc.cdc.gov/eid/article/32/12/25-1991_article"), "10.3201/eid3212.251991")
        self.assertEqual(util.doi_from_url("https://www.cdc.gov/mmwr/volumes/75/wr/mm7541a2.htm"), "10.15585/mmwr.mm7541a2")
        self.assertEqual(util.normalize_doi("https://doi.org/10.2807/ES.1"), "10.2807/es.1")


class KeywordTests(unittest.TestCase):
    def test_title_keywords(self):
        kws = keywords.from_title("Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo, Kenya and Nigeria")
        self.assertTrue(3 <= len(kws) <= 5)
        self.assertIn("Bundibugyo virus", kws)
        self.assertNotIn("caused", " ".join(kws).split())

    def test_drops_dates(self):
        kws = keywords.from_title("Measles Outbreak - United States, April-September 2025")
        self.assertNotIn("April-September", kws)
        self.assertNotIn("2025", kws)

    def test_native_preferred(self):
        self.assertEqual(keywords.pick(["Dengue", "dengue", " Vector "], "Whatever title here"), ["Dengue", "Vector"])
        self.assertTrue(keywords.pick(["MMWR"], "Pertussis vaccine effectiveness", ignore=["mmwr"]))


class StoreTests(unittest.TestCase):
    def test_upsert_dedupe_update_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "db.sqlite")
            r = good_record(summary=None)
            self.assertEqual(st.upsert([r], NOW)["new"], 1)
            self.assertEqual(st.upsert([r], NOW)["unchanged"], 1)
            self.assertEqual(st.upsert([dict(r, summary="S")], NOW)["updated"], 1)
            # 空值不覆盖已有内容
            st.upsert([dict(r, summary=None)], NOW)
            self.assertEqual(st.records()[0]["summary"], "S")
            self.assertEqual(st.mark_source_failed("x", NOW, "boom"), 1)
            self.assertEqual(st.records()[0]["status"], "stale")
            st.mark_source_ok("x", NOW, 1, [])
            self.assertEqual(st.records()[0]["status"], "ok")
            st.close()


class RunnerTests(unittest.TestCase):
    def test_failure_isolated_and_stale(self):
        cfg = runner.load_config(ROOT / "sources.yaml")
        srcs = {s["id"]: s for s in cfg["sources"]}
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "db.sqlite")
            ok_http = FakeHttp({"https://www.who.int/": fixture("who_don.json"),
                                "https://journals.plos.org/": fixture("plos.xml")})
            r1 = runner.collect_source(dict(srcs["who_don"]), ok_http, st, NOW)
            r2 = runner.collect_source(dict(srcs["plos_ntds"]), ok_http, st, NOW)
            self.assertTrue(r1["ok"] and r2["ok"])
            # 第二天 WHO 挂了：WHO 数据保留并标 stale，PLOS 正常
            down = FakeHttp({"https://journals.plos.org/": fixture("plos.xml")})
            r3 = runner.collect_source(dict(srcs["who_don"]), down, st, NOW)
            r4 = runner.collect_source(dict(srcs["plos_ntds"]), down, st, NOW)
            self.assertFalse(r3["ok"])
            self.assertEqual(r3["stale"], 3)
            self.assertTrue(r4["ok"])
            status = runner.export(st, Path(d), window_days=36500, results=[r3, r4])
            lines = [json.loads(x) for x in (Path(d) / "latest.jsonl").read_text().splitlines()]
            self.assertEqual(len(lines), 6)
            for rec in lines:
                self.assertEqual(validate(rec), [])
                self.assertEqual(rec["status"], "stale" if rec["source"] == "who_don" else "ok")
            self.assertEqual(status["runs"]["who_don"]["last_status"], "failed")
            self.assertEqual(len(list((Path(d) / "exports").glob("*.jsonl"))), 1)
            st.close()

    def test_config_loads(self):
        cfg = runner.load_config(ROOT / "sources.yaml")
        ids = [s["id"] for s in cfg["sources"]]
        self.assertEqual(ids, ["mmwr", "eurosurveillance", "who_don", "eid", "ecdc", "plos_ntds", "europepmc"])
        for s in cfg["sources"]:
            self.assertIn(s["kind"], ("article", "news"))
            self.assertIn("license", s)
            self.assertIn("figures_allowed", s)


if __name__ == "__main__":
    unittest.main()
