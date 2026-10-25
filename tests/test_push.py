"""push：增量推送、断点续传、删除标记、对账、通道签名（离线，订阅方用内存假实现）。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from health_sources import push as P
from health_sources.store import Store
from tests.test_core import good_record

NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)


class FakeSubscriber:
    """与 CDC-Weekly-Digest cloudfunctions/hs-ingest 相同语义的内存实现。"""

    def __init__(self, fail_on_upsert: int | None = None):
        self.items: dict[str, dict] = {}
        self.state = {"records": None, "deleted": None}
        self.heartbeats: list[dict] = []
        self.upserts = 0
        self.fail_on_upsert = fail_on_upsert
        self.writes = 0
        self.bodies: list[dict] = []

    def call(self, body):
        self.bodies.append(body)
        a = body["action"]
        if a == "cursor":
            return dict(self.state)
        if a == "upsert":
            self.upserts += 1
            if self.fail_on_upsert == self.upserts:
                raise P.TransportError("boom")
            written = skipped = 0
            for d in body["deleted"]:
                if d["id"] in self.items and not self.items[d["id"]].get("deleted"):
                    self.items[d["id"]] = {**self.items[d["id"]], "deleted": True}
                    self.writes += 1
            for r in body["records"]:
                old = self.items.get(r["id"])
                if old and not old.get("deleted") and old["content_hash"] == r["content_hash"]:
                    skipped += 1
                    continue
                self.items[r["id"]] = {**r, "deleted": False}
                written += 1
                self.writes += 1
            if body.get("cursor"):
                self.state = dict(body["cursor"])
            return {"written": written, "skipped": skipped, "deleted": len(body["deleted"])}
        if a == "heartbeat":
            self.heartbeats.append(body)
            return {}
        if a == "reconcile":
            have = {i: h for i, h in body["manifest"]}
            missing = [i for i in have if i not in self.items or self.items[i].get("deleted")]
            changed = [i for i in have if i in self.items and not self.items[i].get("deleted") and self.items[i]["content_hash"] != have[i]]
            marked = 0
            for i, doc in self.items.items():
                if i not in have and not doc.get("deleted") and (doc.get("sort_key") or "") >= body["windowSince"]:
                    doc["deleted"] = True
                    marked += 1
            return {"missing": missing, "changed": changed, "markedDeleted": marked}
        raise AssertionError(a)

    def live(self):
        return {i: d for i, d in self.items.items() if not d.get("deleted")}


def rid(n: int) -> str:
    return f"{n:016x}"


def recs(n, start=1, source="eid", date="2026-10-20T00:00:00+00:00"):
    return [good_record(id=rid(i), source=source, title=f"T{i}", url=f"https://e.org/{i}", published_at=date,
                        summary=f"S{i}") for i in range(start, start + n)]


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "hs.sqlite"
        self.store = Store(self.db)
        self.sub = P.Subscriber(name="cdc", transport="fake", batch_size=4, max_requests_per_run=20)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ro(self):
        return Store(self.db, readonly=True)

    def test_full_then_incremental_then_idle(self):
        self.store.upsert(recs(10), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        s = P.push(self.ro(), fake, self.sub, "2026-10-24T03:01:00+00:00", now=NOW)
        self.assertTrue(s["complete"])
        self.assertEqual((s["records"], s["written"], s["requests"]), (10, 10, 1 + 3 + 1))   # cursor + 3 批 + 心跳
        self.assertEqual(len(fake.live()), 10)
        self.assertEqual(fake.state["records"], {"updatedAt": "2026-10-24T03:00:00+00:00", "id": rid(10)})
        doc = fake.items[rid(3)]
        self.assertNotIn("fetched_at", doc)
        self.assertNotIn("status", doc)
        self.assertEqual(doc["hs_updated_at"], "2026-10-24T03:00:00+00:00")
        self.assertEqual(doc["sort_key"], "2026-10-20T00:00:00+00:00")
        self.assertEqual(doc["content_hash"], P.content_hash({k: v for k, v in doc.items() if k != "deleted"}))
        self.assertEqual(fake.heartbeats[-1]["exportGeneratedAt"], "2026-10-24T03:01:00+00:00")
        # 第二天：同样内容再抓到（只变 fetched_at）→ 不推；一条改了摘要 → 只推这一条
        again = recs(10)
        for r in again:
            r["fetched_at"] = "2026-10-25T03:00:00+00:00"
        again[4]["summary"] = "changed"
        self.store.upsert(again, "2026-10-25T03:00:00+00:00")
        w = fake.writes
        s2 = P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual((s2["records"], s2["requests"], fake.writes - w), (1, 3, 1))
        self.assertEqual(fake.items[rid(5)]["summary"], "changed")
        s3 = P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual((s3["records"], s3["requests"]), (0, 2))   # 只有 cursor + 心跳

    def test_resume_after_failure_without_duplicates(self):
        self.store.upsert(recs(10), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber(fail_on_upsert=2)
        with self.assertRaises(P.TransportError):
            P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(len(fake.live()), 4)
        self.assertEqual(fake.state["records"]["id"], rid(4))
        fake.fail_on_upsert = None
        s = P.push(self.ro(), fake, self.sub, now=NOW)   # 本地不存任何状态：从订阅方游标续传
        self.assertEqual((s["records"], s["skipped"]), (6, 0))
        self.assertEqual(sorted(fake.live()), [rid(i) for i in range(1, 11)])

    def test_cursor_ties_on_same_updated_at_are_not_skipped(self):
        self.store.upsert(recs(9), "2026-10-24T03:00:00+00:00")   # 9 条同一 updated_at，批大小 4
        fake = FakeSubscriber()
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(len(fake.live()), 9)

    def test_max_requests_per_run_leaves_rest_for_next_run(self):
        self.store.upsert(recs(10), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        self.sub.max_requests_per_run = 2
        s = P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertFalse(s["complete"])
        self.assertEqual(fake.heartbeats[-1]["runStatus"], "partial")
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(len(fake.live()), 10)

    def test_window(self):
        self.store.upsert(recs(2) + recs(1, start=50, date="2024-01-01T00:00:00+00:00"), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(sorted(fake.live()), [rid(1), rid(2)])

    def test_tombstones_retired_and_rekeyed(self):
        self.store.upsert(recs(3) + recs(2, start=20, source="old"), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(len(fake.live()), 5)
        self.store.purge_source("old", "2026-10-25T03:00:00+00:00")
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual(sorted(fake.live()), [rid(1), rid(2), rid(3)])
        self.assertTrue(fake.items[rid(20)]["deleted"])
        self.assertEqual(fake.state["deleted"], {"deletedAt": "2026-10-25T03:00:00+00:00", "id": rid(21)})
        # 删过又回来：删除标记作废，新记录覆盖
        self.store.upsert(recs(1, start=20, source="old"), "2026-10-26T03:00:00+00:00")
        P.push(self.ro(), fake, self.sub, now=NOW)
        self.assertFalse(fake.items[rid(20)]["deleted"])

    def test_backfill_dois_rekey_pushes_new_id_and_deletes_old(self):
        r = good_record(id=rid(7), source="mmwr", url="https://www.cdc.gov/mmwr/volumes/74/wr/mm7401a1.htm",
                        published_at="2026-10-20T00:00:00+00:00", summary="S")
        self.store.upsert([r], "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        P.push(self.ro(), fake, self.sub, now=NOW)
        out = self.store.backfill_dois(["mmwr"], lambda url: "10.15585/mmwr.mm7401a1", "2026-10-25T03:00:00+00:00")
        self.assertEqual(out["rekeyed"], 1)
        P.push(self.ro(), fake, self.sub, now=NOW)
        live = fake.live()
        self.assertEqual(len(live), 1)
        (new_id, doc), = live.items()
        self.assertNotEqual(new_id, rid(7))
        self.assertEqual(doc["doi"], "10.15585/mmwr.mm7401a1")
        self.assertTrue(fake.items[rid(7)]["deleted"])

    def test_reconcile_resends_missing_and_changed_and_marks_extra_deleted(self):
        self.store.upsert(recs(5), "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        P.push(self.ro(), fake, self.sub, now=NOW)
        del fake.items[rid(1)]
        fake.items[rid(2)]["content_hash"] = "x"
        fake.items[rid(99)] = {"id": rid(99), "content_hash": "y", "sort_key": "2026-10-01T00:00:00+00:00", "deleted": False}
        fake.items[rid(98)] = {"id": rid(98), "content_hash": "y", "sort_key": "2020-01-01T00:00:00+00:00", "deleted": False}
        w = fake.writes
        s = P.reconcile(self.ro(), fake, self.sub, now=NOW)
        self.assertEqual((s["manifest"], s["missing"], s["changed"], s["resent"], s["markedDeleted"]), (5, 1, 1, 2, 1))
        self.assertEqual(fake.writes - w, 2)
        self.assertEqual(sorted(fake.live()), [rid(i) for i in range(1, 6)] + [rid(98)])   # 窗口外的不动
        self.assertEqual(fake.state["records"]["id"], rid(5))   # 对账补推不动游标

    def test_readonly_store_cannot_write_and_tolerates_old_db(self):
        conn = sqlite3.connect(str(Path(self.tmp.name) / "old.sqlite"))
        conn.execute("CREATE TABLE records (id TEXT)")
        conn.close()
        old = Store(Path(self.tmp.name) / "old.sqlite", readonly=True)
        self.assertEqual(old.tombstones_since(None, 10), [])
        old.close()
        ro = self.ro()
        with self.assertRaises(sqlite3.OperationalError):
            ro.conn.execute("CREATE TABLE x (a)")
        ro.close()

    def test_body_size_limit(self):
        big = recs(4)
        for r in big:
            r["summary"] = "x" * 3000
        self.store.upsert(big, "2026-10-24T03:00:00+00:00")
        fake = FakeSubscriber()
        self.sub.max_body_bytes = 8000
        P.push(self.ro(), fake, self.sub, now=NOW)
        sizes = [len(b["records"]) for b in fake.bodies if b["action"] == "upsert"]
        self.assertEqual(sizes, [2, 2])
        self.assertEqual(len(fake.live()), 4)


class FakeHttpResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.sent = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.sent.append({"url": url, "data": data, "headers": headers})
        return FakeHttpResp(self.response)


class TransportTests(unittest.TestCase):
    def test_scf_invoke_signs_and_unwraps(self):
        sess = FakeSession({"Response": {"Result": {"InvokeResult": 0, "RetMsg": json.dumps({"ok": True, "data": {"records": None}})}}})
        t = P.ScfInvokeTransport("AKIDtest", "keytest", namespace="env-1", function="hs-ingest", token="tok",
                                 session=sess, clock=lambda: 1761360000)
        self.assertEqual(t.call({"action": "cursor"}), {"records": None})
        sent = sess.sent[0]
        body = json.loads(sent["data"])
        self.assertEqual((body["FunctionName"], body["Namespace"], body["InvocationType"]), ("hs-ingest", "env-1", "RequestResponse"))
        self.assertEqual(json.loads(body["ClientContext"]), {"action": "cursor", "token": "tok"})
        h = sent["headers"]
        self.assertEqual(h["X-TC-Action"], "Invoke")
        self.assertTrue(h["Authorization"].startswith("TC3-HMAC-SHA256 Credential=AKIDtest/2025-10-25/scf/tc3_request, SignedHeaders=content-type;host, Signature="))
        self.assertNotIn("keytest", json.dumps(sent, default=lambda b: b.decode()))

    def test_scf_errors_do_not_leak_secrets(self):
        for resp in ({"Response": {"Error": {"Code": "AuthFailure", "Message": "denied"}}},
                     {"Response": {"Result": {"InvokeResult": 1, "ErrMsg": "crash"}}},
                     {"Response": {"Result": {"InvokeResult": 0, "RetMsg": json.dumps({"ok": False, "error": {"code": "FORBIDDEN"}})}}}):
            t = P.ScfInvokeTransport("AKIDtest", "keytest", namespace="e", function="f", session=FakeSession(resp))
            with self.assertRaises(P.TransportError) as cm:
                t.call({"action": "cursor"})
            self.assertNotIn("keytest", str(cm.exception))

    def test_hmac_transport(self):
        sess = FakeSession({"ok": True, "data": {"x": 1}})
        t = P.HmacHttpsTransport("https://example.org/hs-ingest", "s3cret", session=sess, clock=lambda: 100)
        self.assertEqual(t.call({"action": "cursor"}), {"x": 1})
        import hashlib, hmac
        sent = sess.sent[0]
        want = hmac.new(b"s3cret", b"100." + sent["data"], hashlib.sha256).hexdigest()
        self.assertEqual(sent["headers"]["X-HS-Signature"], want)

    def test_load_subscriber_prefers_systemd_credentials(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            Path(d, "cdc.toml").write_text('transport = "tencent-scf-invoke"\nbatch_size = 50\ncredentials_file = "/nonexistent"\n'
                                           '[tencent-scf-invoke]\nnamespace = "env-1"\nfunction = "hs-ingest"\n', encoding="utf-8")
            cred = Path(d, "creds")
            cred.mkdir()
            (cred / "cdc.secret").write_text("TENCENTCLOUD_SECRETID=AKIDx\nTENCENTCLOUD_SECRETKEY=k\n", encoding="utf-8")
            os.environ["CREDENTIALS_DIRECTORY"] = str(cred)
            try:
                sub = P.load_subscriber("cdc", d)
            finally:
                del os.environ["CREDENTIALS_DIRECTORY"]
            self.assertEqual((sub.batch_size, sub.options["namespace"], sub.credentials["TENCENTCLOUD_SECRETID"]), (50, "env-1", "AKIDx"))
            self.assertIsInstance(P.make_transport(sub), P.ScfInvokeTransport)


class CliTests(unittest.TestCase):
    def test_dry_run_needs_no_credentials(self):
        import contextlib, io
        from health_sources.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "health_sources.sqlite")
            st.upsert(recs(3), "2026-10-24T03:00:00+00:00")
            st.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--data-dir", d, "push", "--subscriber", "cdc", "--config-dir", d, "--dry-run"])
            out = json.loads(buf.getvalue())
            self.assertEqual((rc, out["records"], out["complete"]), (0, 3, True))


if __name__ == "__main__":
    unittest.main()
