import json
import tempfile
import unittest
from pathlib import Path

from health_sources import backfill as bf
from health_sources import runner
from health_sources.schema import validate
from health_sources.store import Store
from tests.helpers import NOW, ROOT, FakeHttp, fixture, source_cfg

NOSLEEP = lambda s: None  # noqa: E731


def ctx(http, since="2026-08-01"):
    return bf.Ctx(http, NOW, since, sleep=NOSLEEP)


def collect(gen):
    return [r for batch in gen for r in batch]


class CcdcIssuesTests(unittest.TestCase):
    def test_issue_range(self):
        self.assertEqual(bf.parse_issue_range("28-30,43,29"), [28, 29, 30, 43])

    def test_historic_issue_feed(self):
        src = source_cfg("cdc_weekly")
        url = "https://weekly.chinacdc.cn/rss/2026_28.xml"
        http = FakeHttp({url: fixture("cdc_weekly_2026_28.xml")})
        c = ctx(http, "2026-01-01")
        recs = collect(bf.bf_ccdc_issues(src, c, {"year": 2026, "issues": "28"}))
        self.assertEqual(http.calls, [url])
        self.assertEqual(len(recs), 5)
        for r in recs:
            self.assertEqual(validate(r), [], r)
            self.assertEqual((r["volume"], r["issue"]), ("8", "28"))
            self.assertEqual(r["feed_url"], url)
        self.assertEqual([r["issue_order"] for r in recs], list(range(5)))

    def test_missing_issue_is_noted_not_fatal(self):
        src = source_cfg("cdc_weekly")
        sleeps = []
        http = FakeHttp({"https://weekly.chinacdc.cn/rss/2026_28.xml": fixture("cdc_weekly_2026_28.xml")})
        c = bf.Ctx(http, NOW, "2026-01-01", sleep=sleeps.append)
        recs = collect(bf.bf_ccdc_issues(src, c, {"year": 2026, "issues": "28-29"}))
        self.assertEqual(len(recs), 5)
        self.assertTrue(any("2026_29" in n for n in c.notes))
        self.assertEqual(sleeps, [6])   # 两次请求之间按配置间隔

    def test_wrong_issue_metadata_dropped(self):
        src = source_cfg("cdc_weekly")
        http = FakeHttp({"https://weekly.chinacdc.cn/rss/2026_30.xml": fixture("cdc_weekly_2026_28.xml")})
        c = ctx(http, "2026-01-01")
        self.assertEqual(collect(bf.bf_ccdc_issues(src, c, {"year": 2026, "issues": "30"})), [])
        self.assertTrue(any("other issue" in n for n in c.notes))


class FullFeedTests(unittest.TestCase):
    def test_streams_in_batches_and_filters_by_date(self):
        src = source_cfg("mmwr")
        src["backfill"] = {**src["backfill"], "batch_items": 1}
        raw = "https://tools.cdc.gov/api/embed/downloader/download.asp?m=342778&c=766718"
        http = FakeHttp({"https://tools.cdc.gov/api/v2": fixture("mmwr.xml")},
                        {raw: "https://www.cdc.gov/mmwr/volumes/75/wr/mm7541a2.htm?s_cid=x"})
        c = ctx(http, "2026-10-22")
        batches = list(bf.bf_full_feed(src, c, {}))
        recs = [r for b in batches for r in b]
        self.assertTrue(recs)
        self.assertTrue(all(r["published_at"][:10] >= "2026-10-22" for r in recs))
        self.assertTrue(all(validate(r) == [] for r in recs))
        self.assertIn("10.15585/mmwr.mm7541a2", {r["doi"] for r in recs})
        self.assertTrue(any("scanned" in n for n in c.notes))

    def test_chunker_handles_split_tags(self):
        class R:
            def iter_content(self, n):
                data = b"<rss><channel><item><a>1</a></item><item><a>2</a></item><item><a>3</a></item></channel></rss>"
                for i in range(0, len(data), 7):
                    yield data[i:i + 7]
        out = list(bf.iter_feed_chunks(R(), 2))
        self.assertEqual([len(items) for _, items in out], [2, 1])
        self.assertEqual(out[0][0], b"<rss><channel>")
        self.assertTrue(all(i.startswith(b"<item>") and i.endswith(b"</item>") for _, items in out for i in items))


class CrossrefTests(unittest.TestCase):
    def test_eid_records(self):
        src = source_cfg("eid")
        http = FakeHttp({"https://api.crossref.org/journals/1080-6059/works": fixture("crossref_eid.json")})
        c = ctx(http)
        recs = collect(bf.bf_crossref(src, c, {}))
        self.assertEqual(len(recs), 3)
        r = recs[0]
        self.assertEqual(validate(r), [])
        self.assertEqual(r["doi"], "10.3201/eid3209.260473")
        self.assertEqual(r["url"], "https://wwwnc.cdc.gov/eid/article/32/9/26-0473_article")
        self.assertEqual((r["volume"], r["issue"]), ("32", "9"))
        self.assertTrue(r["authors"])
        self.assertNotIn("<b>", r["title"])
        # 只有「年-月」→ 用 DOI 注册时间（fixture 里 created 存在时）或当月 1 日
        self.assertTrue(r["published_at"].startswith("2026-0"))
        self.assertIn("from-pub-date:2026-08-01", http.params[0]["filter"])
        # 不足一页就停，不再翻页
        self.assertEqual(len(http.calls), 1)

    def test_date_rules(self):
        today = "2026-10-25T00:00:00+00:00"
        self.assertEqual(bf._date_parts({"published-online": {"date-parts": [[2026, 8, 20]]}}, today), "2026-08-20T00:00:00+00:00")
        self.assertEqual(bf._date_parts({"published-print": {"date-parts": [[2026, 11]]},
                                         "created": {"date-time": "2026-10-10T00:41:47Z"}}, today), "2026-10-10T00:41:47+00:00")
        self.assertEqual(bf._date_parts({"published-print": {"date-parts": [[2026, 9]]}}, today), "2026-09-01T00:00:00+00:00")
        # 2109 年（元数据录错）跳过，回落到 created
        self.assertEqual(bf._date_parts({"published-print": {"date-parts": [[2109, 2]]},
                                         "created": {"date-time": "2019-01-07T02:01:51Z"}}, today), "2019-01-07T02:01:51+00:00")
        self.assertIsNone(bf._date_parts({"published-print": {"date-parts": [[2109, 2]]}}, today))

    def test_eid_url(self):
        self.assertEqual(bf.eid_url("10.3201/eid3212.251991"), "https://wwwnc.cdc.gov/eid/article/32/12/25-1991_article")
        self.assertIsNone(bf.eid_url("10.2807/x"))

    def test_euro_prefix_url_and_jats_abstract(self):
        src = source_cfg("eurosurveillance")
        item = {"DOI": "10.2807/1560-7917.ES.2026.31.33.2500001", "title": ["Hello"],
                "published-online": {"date-parts": [[2026, 8, 20]]}, "volume": "31", "issue": "33",
                "abstract": "<jats:sec><jats:title>AIM</jats:title><jats:p>We aimed.</jats:p></jats:sec>"}
        r = bf.crossref_record(item, src, src["backfill"], NOW)
        self.assertEqual(validate(r), [])
        self.assertEqual(r["url"], "https://www.eurosurveillance.org/content/10.2807/1560-7917.ES.2026.31.33.2500001")
        self.assertEqual(r["summary"], "We aimed.")
        self.assertEqual(r["published_at"], "2026-08-20T00:00:00+00:00")


class PagedTests(unittest.TestCase):
    def test_who_don_stops_at_since(self):
        src = source_cfg("who_don")
        http = FakeHttp({"https://www.who.int/api/news/diseaseoutbreaknews": fixture("who_don.json")})
        c = ctx(http, "2000-01-01")
        recs = collect(bf.bf_odata_pages(src, c, {}))
        self.assertEqual(len(recs), 3)
        self.assertEqual(len(http.calls), 1)          # 不足一页就停
        self.assertEqual(http.params[0]["$skip"], "0")
        self.assertTrue(all(r["feed_url"] for r in recs))

    def test_ecdc_pages_until_repeat(self):
        src = source_cfg("ecdc")
        src["feeds"] = src["feeds"][:1]
        http = FakeHttp({src["feeds"][0]["url"]: fixture("ecdc_risk.xml")})
        c = ctx(http, "2000-01-01")
        recs = collect(bf.bf_feed_pages(src, c, {}))
        # 第二页和第一页相同（服务器忽略分页）→ 停止，不重复计数
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(http.calls[1].endswith("?page=1"))
        self.assertEqual(len(recs), 2)
        self.assertTrue(any("pagination ignored" in n for n in c.notes))

    def test_plos_search(self):
        src = source_cfg("plos_ntds")
        http = FakeHttp({"https://api.plos.org/search": fixture("plos_search.json")})
        c = ctx(http)
        recs = collect(bf.bf_plos_search(src, c, {}))
        self.assertEqual(len(recs), 2)
        r = recs[0]
        self.assertEqual(validate(r), [])
        self.assertTrue(r["url"].startswith("https://journals.plos.org/plosntds/article?id=10.1371/journal.pntd."))
        self.assertTrue(r["summary"])
        self.assertTrue(r["article_type"])
        self.assertIn("doc_type:full", http.params[0]["fq"])


class BackfillRunTests(unittest.TestCase):
    def test_run_backfill_writes_store_report_and_by_source_export(self):
        with tempfile.TemporaryDirectory() as d:
            http = FakeHttp({"https://weekly.chinacdc.cn/rss/2026_28.xml": fixture("cdc_weekly_2026_28.xml")})
            res = runner.run_backfill(ROOT / "sources.yaml", d, ["cdc_weekly"], "2026-01-01",
                                      {"year": 2026, "issues": "28"}, http=http, sleep=NOSLEEP)
            self.assertEqual(res[0]["records"], 5)
            self.assertEqual(res[0]["new"], 5)
            self.assertTrue((Path(d) / "backfill" / "cdc_weekly.json").exists())
            lines = [json.loads(x) for x in (Path(d) / "by-source" / "cdc_weekly.jsonl").read_text().splitlines()]
            self.assertEqual(len(lines), 5)
            self.assertTrue(all(validate(x) == [] for x in lines))
            status = json.loads((Path(d) / "status.json").read_text())
            self.assertEqual(status["by_source"]["cdc_weekly"]["records"], 5)
            self.assertNotIn("cdc_weekly", status["runs"])   # 回补不冒充每日采集状态
            # 再跑一次：全部 unchanged，不重复
            res2 = runner.run_backfill(ROOT / "sources.yaml", d, ["cdc_weekly"], "2026-01-01",
                                       {"year": 2026, "issues": "28"}, http=http, sleep=NOSLEEP)
            self.assertEqual((res2[0]["new"], res2[0]["unchanged"]), (0, 5))

    def test_source_without_entry_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "x.sqlite")
            src = {"id": "x", "kind": "article"}
            out = bf.backfill_source(src, store, FakeHttp({}), NOW, "2026-08-01")
            store.close()
            self.assertTrue(out["skipped"])

    def test_failure_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            res = runner.run_backfill(ROOT / "sources.yaml", d, ["who_don", "plos_ntds"], "2026-08-01",
                                      http=FakeHttp({"https://api.plos.org/search": fixture("plos_search.json")}),
                                      sleep=NOSLEEP)
            by = {r["source"]: r for r in res}
            self.assertFalse(by["who_don"]["ok"])
            self.assertTrue(by["plos_ntds"]["ok"])

    def test_old_db_migrates_new_columns(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.sqlite"
            conn = sqlite3.connect(p)
            conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, source TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,"
                         " lang TEXT NOT NULL, published_at TEXT, url TEXT NOT NULL, doi TEXT, summary TEXT, keywords TEXT NOT NULL,"
                         " license TEXT, figures_allowed INTEGER NOT NULL, fetched_at TEXT NOT NULL, status TEXT NOT NULL,"
                         " first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.commit(); conn.close()
            s = Store(p)
            cols = {r[1] for r in s.conn.execute("PRAGMA table_info(records)")}
            s.close()
            self.assertTrue({"volume", "issue", "authors", "issue_order", "feed_url"} <= cols)


if __name__ == "__main__":
    unittest.main()
