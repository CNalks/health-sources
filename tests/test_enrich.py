"""补全步骤（按 DOI 回查 Europe PMC）的离线测试。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from health_sources import enrich, keywords, runner
from health_sources.schema import validate
from health_sources.store import Store
from tests.helpers import NOW, ROOT, FakeHttp, FakeResponse, fixture

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class DoiHttp:
    """离线 Europe PMC：按查询里的 DOI 返回 fixture；未登记的 DOI 返回空结果；errors 里的 DOI 抛错。"""

    def __init__(self, by_doi: dict[str, bytes], errors: set[str] | None = None):
        self.by_doi = by_doi
        self.errors = errors or set()
        self.queries: list[str] = []

    def get(self, url, params=None, stream=False):
        assert url == EPMC
        doi = params["query"][len('DOI:"'):-1]
        self.queries.append(doi)
        if doi in self.errors:
            raise ConnectionError("boom")
        return FakeResponse(self.by_doi.get(doi, fixture("epmc_doi_empty.json")))


def rec(source="cdc_weekly", doi="10.46234/ccdcw2026.200", summary=None, license="CC BY-NC 4.0",
        kind="article", published=NOW, title="Genomic Epidemiology of Human Brucellosis in Shenzhen",
        figures=False, **over):
    from health_sources.util import make_id
    r = {
        "id": make_id(doi, "https://e.org/" + (doi or title)), "source": source, "kind": kind, "title": title,
        "lang": "en", "published_at": published, "url": "https://e.org/" + (doi or title), "doi": doi,
        "summary": summary, "keywords": keywords.pick(None, title), "license": license,
        "figures_allowed": figures, "fetched_at": NOW, "status": "ok",
        "pmcid": None, "enriched_at": None, "enrich_source": None,
    }
    r.update(over)
    return r


SOURCES = {
    "cdc_weekly": {"id": "cdc_weekly", "kind": "article", "figures_allowed": False},
    "mmwr": {"id": "mmwr", "kind": "article", "figures_allowed": True},
    "perrec": {"id": "perrec", "kind": "article", "figures_allowed": "per_record"},
    "who_don": {"id": "who_don", "kind": "news", "figures_allowed": False},
}
CONF = {"max_per_run": 60, "delay_seconds": 0}


def later(hours: float) -> str:
    return (datetime.fromisoformat(NOW) + timedelta(hours=hours)).isoformat()


class MergeTests(unittest.TestCase):
    def test_merge_med_and_pmc_without_doi(self):
        out = enrich.merge_results(fixture("epmc_doi_found.json"), "10.46234/ccdcw2026.200")
        self.assertTrue(out["summary"].startswith("What is already known"))  # HTML 标签已去掉
        self.assertNotIn("<h4>", out["summary"])
        self.assertEqual(out["license"], "CC BY-NC")
        self.assertEqual(out["pmcid"], "PMC13673392")  # 来自不带 doi 字段的 PMC 那条
        self.assertIn("Brucellosis", out["keywords"])

    def test_merge_empty_and_mismatch(self):
        self.assertIsNone(enrich.merge_results(fixture("epmc_doi_empty.json"), "10.1/x"))
        self.assertIsNone(enrich.merge_results(fixture("epmc_doi_noabstract.json"), "10.1/other"))
        with self.assertRaises(ValueError):
            enrich.merge_results(b'{"error": 1}', "10.1/x")

    def test_figures_licenses(self):
        allowed = ["cc0", "cc by", "cc by-sa"]
        self.assertTrue(enrich.figures_allowed("CC BY", allowed))
        self.assertTrue(enrich.figures_allowed("cc by-sa", allowed))
        self.assertTrue(enrich.figures_allowed("CC0", allowed))
        self.assertFalse(enrich.figures_allowed("CC BY-NC", allowed))
        self.assertFalse(enrich.figures_allowed("CC BY-NC-ND", allowed))
        self.assertFalse(enrich.figures_allowed(None, allowed))


class EnrichRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def get(self, rid):
        return {r["id"]: r for r in self.store.records()}[rid]

    def test_fills_only_empty_fields_and_keeps_source(self):
        r = rec()
        self.store.upsert([r], NOW)
        http = DoiHttp({"10.46234/ccdcw2026.200": fixture("epmc_doi_found.json")})
        st = enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        self.assertEqual((st["attempted"], st["found"], st["summary_filled"]), (1, 1, 1))
        got = self.get(r["id"])
        self.assertEqual(validate(got), [])
        self.assertTrue(got["summary"])
        self.assertEqual(got["license"], "CC BY-NC 4.0")    # 已有许可不被覆盖
        self.assertEqual(got["pmcid"], "PMC13673392")
        self.assertIn("Brucellosis", got["keywords"])        # 原来是标题自动提取的 → 换成原文关键词
        self.assertEqual(got["source"], "cdc_weekly")
        self.assertFalse(got["figures_allowed"])             # 非 per_record 来源不重算
        self.assertEqual((got["enriched_at"], got["enrich_source"]), (NOW, "europepmc"))
        # 补齐后不再是候选
        self.assertEqual(enrich.run(self.store, http, CONF, SOURCES, later(48), sleep=lambda s: None)["candidates"], 0)

    def test_native_keywords_not_overwritten(self):
        r = rec(keywords=["Original Keyword"])
        self.store.upsert([r], NOW)
        http = DoiHttp({"10.46234/ccdcw2026.200": fixture("epmc_doi_found.json")})
        enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        self.assertEqual(self.get(r["id"])["keywords"], ["Original Keyword"])

    def test_feed_refetch_does_not_undo_enrichment(self):
        r = rec()
        self.store.upsert([r], NOW)
        http = DoiHttp({"10.46234/ccdcw2026.200": fixture("epmc_doi_found.json")})
        enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        before = self.get(r["id"])
        stats = self.store.upsert([dict(r, fetched_at=later(24))], later(24))  # 第二天 feed 又抓到同一篇
        after = self.get(r["id"])
        self.assertEqual(stats["unchanged"], 1)
        for f in ("summary", "keywords", "pmcid", "enriched_at", "enrich_source"):
            self.assertEqual(after[f], before[f], f)

    def test_per_record_recomputes_figures(self):
        r = rec(source="perrec", license=None, figures=True)
        self.store.upsert([r], NOW)
        http = DoiHttp({"10.46234/ccdcw2026.200": fixture("epmc_doi_found.json")})
        st = enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        got = self.get(r["id"])
        self.assertEqual(got["license"], "CC BY-NC")
        self.assertFalse(got["figures_allowed"])   # NC 不允许配图
        self.assertEqual(st["figures_changed"], 1)

    def test_candidate_selection(self):
        old = (datetime.fromisoformat(NOW) - timedelta(days=31)).isoformat()
        rows = [
            rec(doi="10.1/a"),                                   # 候选：缺摘要
            rec(doi="10.1/b", summary="S", license=None),        # 候选：缺许可
            rec(doi="10.1/c", summary="S"),                      # 不是：都齐了
            rec(doi=None, title="No doi article here"),          # 不是：没 DOI
            rec(doi="10.1/d", published=old),                    # 不是：发布超过 30 天
            rec(doi="10.1/e", kind="news", source="who_don"),    # 不是：news
            rec(doi="10.1/f", source="retired_src"),             # 不是：来源不在配置里
        ]
        self.store.upsert(rows, NOW)
        http = DoiHttp({})
        st = enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        self.assertEqual(sorted(http.queries), ["10.1/a", "10.1/b"])
        self.assertEqual(st["not_found"], 2)

    def test_retry_next_day_within_30_days(self):
        r = rec(doi="10.1/late")
        self.store.upsert([r], NOW)
        http = DoiHttp({})
        enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        enrich.run(self.store, http, CONF, SOURCES, later(2), sleep=lambda s: None)    # 同一天：不重试
        self.assertEqual(http.queries, ["10.1/late"])
        enrich.run(self.store, http, CONF, SOURCES, later(23.9), sleep=lambda s: None)  # 第二天（20h 后）重试
        self.assertEqual(len(http.queries), 2)
        # 第 12 天收录了
        http.by_doi["10.1/late"] = fixture("epmc_doi_found.json").replace(b"10.46234/ccdcw2026.200", b"10.1/late")
        st = enrich.run(self.store, http, CONF, SOURCES, later(24 * 12), sleep=lambda s: None)
        self.assertEqual(st["summary_filled"], 1)
        att = self.store.conn.execute("SELECT attempts, last_result FROM enrich_attempts").fetchone()
        self.assertEqual((att["attempts"], att["last_result"]), (3, "filled"))
        # 超过 30 天的不再尝试
        self.store.upsert([rec(doi="10.1/never")], NOW)
        enrich.run(self.store, http, CONF, SOURCES, later(24 * 31), sleep=lambda s: None)
        self.assertNotIn("10.1/never", http.queries[4:])

    def test_found_without_abstract_is_partial_and_retried(self):
        r = rec(source="mmwr", doi="10.15585/mmwr.mm7541a2", license="Public Domain (U.S. CDC)")
        self.store.upsert([r], NOW)
        http = DoiHttp({"10.15585/mmwr.mm7541a2": fixture("epmc_doi_noabstract.json")})
        st = enrich.run(self.store, http, CONF, SOURCES, NOW, sleep=lambda s: None)
        self.assertEqual((st["found"], st["summary_filled"]), (1, 0))
        att = self.store.conn.execute("SELECT last_result FROM enrich_attempts").fetchone()
        self.assertEqual(att["last_result"], "found_nothing_new")
        self.assertIsNone(self.get(r["id"])["enriched_at"])
        st = enrich.run(self.store, http, CONF, SOURCES, later(24), sleep=lambda s: None)
        self.assertEqual(st["attempted"], 1)  # 仍缺摘要，隔天再查

    def test_limit_sleep_and_errors_isolated(self):
        self.store.upsert([rec(doi=f"10.1/n{i}") for i in range(5)], NOW)
        slept = []
        http = DoiHttp({}, errors={"10.1/n0", "10.1/n1", "10.1/n2", "10.1/n3", "10.1/n4"})
        st = enrich.run(self.store, http, {**CONF, "max_per_run": 3, "delay_seconds": 1.5}, SOURCES, NOW,
                        sleep=slept.append)
        self.assertEqual((st["candidates"], st["attempted"], st["errors"]), (3, 3, 3))
        self.assertEqual(slept, [1.5, 1.5])  # 请求之间有间隔
        # 下一次（隔天）先查从没试过的
        http2 = DoiHttp({})
        enrich.run(self.store, http2, {**CONF, "max_per_run": 2}, SOURCES, later(24), sleep=lambda s: None)
        tried_first = set(http.queries)
        self.assertFalse(set(http2.queries) & tried_first)

    def test_disabled(self):
        self.store.upsert([rec()], NOW)
        http = DoiHttp({})
        st = enrich.run(self.store, http, {"enabled": False}, SOURCES, NOW)
        self.assertFalse(st["enabled"])
        self.assertEqual(http.queries, [])


class RunnerIntegrationTests(unittest.TestCase):
    def test_retire_migrate_collect_enrich_export(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "health_sources.sqlite"
            # v0.1 旧库：没有新列，还有 europepmc 记录
            con = sqlite3.connect(db)
            con.executescript("""
                CREATE TABLE records (id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
                  lang TEXT NOT NULL, published_at TEXT, url TEXT NOT NULL, doi TEXT, summary TEXT, keywords TEXT NOT NULL,
                  license TEXT, figures_allowed INTEGER NOT NULL, fetched_at TEXT NOT NULL, status TEXT NOT NULL,
                  first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE source_runs (source TEXT PRIMARY KEY, last_attempt_at TEXT, last_success_at TEXT,
                  last_status TEXT, last_error TEXT, last_count INTEGER, warnings TEXT);
            """)
            con.execute("INSERT INTO records VALUES ('aaaaaaaaaaaaaaaa','europepmc','article','Old','en',?,"
                        "'https://europepmc.org/article/PMC/PMC1',NULL,NULL,'[]','CC BY',1,?,'ok',?,?)",
                        (NOW, NOW, NOW, NOW))
            con.execute("INSERT INTO source_runs(source,last_status) VALUES ('europepmc','ok')")
            con.commit()
            con.close()

            cfg_path = Path(d) / "sources.yaml"
            import yaml
            cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
            cfg["sources"] = [s for s in cfg["sources"] if s["id"] == "cdc_weekly"]
            cfg["enrich"]["delay_seconds"] = 0
            cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

            feed_http = FakeHttp({"https://weekly.chinacdc.cn/rss/current.xml": fixture("cdc_weekly.xml")})
            epmc = DoiHttp({"10.46234/ccdcw2026.213": fixture("epmc_doi_found.json").replace(
                b"10.46234/ccdcw2026.200", b"10.46234/ccdcw2026.213")})
            orig = runner.Http
            runner.Http = lambda *a, **k: feed_http
            try:
                results = runner.run(cfg_path, d, enrich_http=epmc)
            finally:
                runner.Http = orig
            self.assertTrue(results[0]["ok"])
            self.assertEqual(len(epmc.queries), 4)
            lines = [json.loads(x) for x in (Path(d) / "latest.jsonl").read_text().splitlines()]
            self.assertEqual({r["source"] for r in lines}, {"cdc_weekly"})   # europepmc 已删除、不导出
            for r in lines:
                self.assertEqual(validate(r), [])
            self.assertEqual(sum(1 for r in lines if r["summary"]), 1)
            status = json.loads((Path(d) / "status.json").read_text())
            self.assertEqual(status["retired_sources"], {"europepmc": 1})
            self.assertNotIn("europepmc", status["runs"])
            self.assertEqual(status["last_enrich"]["summary_filled"], 1)
            self.assertEqual(status["sources"]["cdc_weekly"]["summary_from_enrich"], 1)
            self.assertEqual(status["enrich_attempts"]["not_found"], 3)


if __name__ == "__main__":
    unittest.main()
