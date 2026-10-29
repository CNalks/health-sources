"""SQLite 存储：按 id 去重和更新；记录每个来源最近一次抓取状态。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schema import FIELDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    lang TEXT NOT NULL,
    published_at TEXT,
    url TEXT NOT NULL,
    doi TEXT,
    summary TEXT,
    keywords TEXT NOT NULL,          -- JSON 数组
    license TEXT,
    figures_allowed INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pmcid TEXT,
    enriched_at TEXT,
    enrich_source TEXT,
    enriched_fields TEXT,             -- JSON 数组：由 enrich 写入的字段，之后 feed 再抓到时不覆盖
    journal TEXT,
    volume TEXT,
    issue TEXT,
    pages TEXT,
    article_type TEXT,
    authors TEXT,                     -- JSON 数组或 NULL
    issue_order INTEGER,
    feed_url TEXT,
    disease_tags TEXT NOT NULL DEFAULT '[]',
    country_iso3 TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source);
CREATE INDEX IF NOT EXISTS idx_records_published ON records(published_at);
CREATE TABLE IF NOT EXISTS source_runs (
    source TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT,                -- ok / failed
    last_error TEXT,
    last_count INTEGER,
    warnings TEXT
);
-- 按 DOI 回查的尝试记录：查不到/没摘要时隔天再试（发布后 30 天内）
CREATE TABLE IF NOT EXISTS enrich_attempts (
    id TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL,
    first_attempt_at TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    last_result TEXT NOT NULL,       -- filled / partial / found_nothing_new / not_found / error
    last_error TEXT
);
-- 删除标记：记录从 records 删掉时留一条，供下游订阅方（push）同步删除；按 (deleted_at, id) 增量读
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    reason TEXT NOT NULL             -- retired / merged / rekeyed
);
CREATE INDEX IF NOT EXISTS idx_tombstones_cursor ON tombstones(deleted_at, id);
CREATE INDEX IF NOT EXISTS idx_records_updated ON records(updated_at, id);
"""

# 旧库（v0.1）缺的列：启动时补上
MIGRATIONS = {
    "pmcid": "ALTER TABLE records ADD COLUMN pmcid TEXT",
    "enriched_at": "ALTER TABLE records ADD COLUMN enriched_at TEXT",
    "enrich_source": "ALTER TABLE records ADD COLUMN enrich_source TEXT",
    "enriched_fields": "ALTER TABLE records ADD COLUMN enriched_fields TEXT",
    "journal": "ALTER TABLE records ADD COLUMN journal TEXT",
    "volume": "ALTER TABLE records ADD COLUMN volume TEXT",
    "issue": "ALTER TABLE records ADD COLUMN issue TEXT",
    "pages": "ALTER TABLE records ADD COLUMN pages TEXT",
    "article_type": "ALTER TABLE records ADD COLUMN article_type TEXT",
    "authors": "ALTER TABLE records ADD COLUMN authors TEXT",
    "issue_order": "ALTER TABLE records ADD COLUMN issue_order INTEGER",
    "feed_url": "ALTER TABLE records ADD COLUMN feed_url TEXT",
    "disease_tags": "ALTER TABLE records ADD COLUMN disease_tags TEXT NOT NULL DEFAULT '[]'",
    "country_iso3": "ALTER TABLE records ADD COLUMN country_iso3 TEXT NOT NULL DEFAULT '[]'",
}

CONTENT_FIELDS = [f for f in FIELDS if f not in ("id", "fetched_at", "status")]


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if readonly:   # 下游推送只读：不建表、不迁移，库不存在就报错
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(records)")}
        if cols:  # 已有旧表：先补列，再跑建表/建索引（都是 IF NOT EXISTS）
            with self.conn:
                for col, sql in MIGRATIONS.items():
                    if col not in cols:
                        self.conn.execute(sql)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _to_row(rec: dict) -> dict:
        row = dict(rec)
        for f in FIELDS:            # v0.2 及更早构造的记录没有卷期字段：按 null 处理
            row.setdefault(f, None)
        row["keywords"] = json.dumps(rec["keywords"], ensure_ascii=False)
        row["disease_tags"] = json.dumps(rec.get("disease_tags") or [], ensure_ascii=False)
        row["country_iso3"] = json.dumps(rec.get("country_iso3") or [], ensure_ascii=False)
        row["figures_allowed"] = int(bool(rec["figures_allowed"]))
        row["authors"] = json.dumps(rec["authors"], ensure_ascii=False) if rec.get("authors") else None
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict:
        rec = {f: row[f] for f in FIELDS}
        rec["keywords"] = json.loads(rec["keywords"])
        rec["disease_tags"] = json.loads(rec["disease_tags"] or "[]")
        rec["country_iso3"] = json.loads(rec["country_iso3"] or "[]")
        rec["figures_allowed"] = bool(rec["figures_allowed"])
        rec["authors"] = json.loads(rec["authors"]) if rec["authors"] else None
        return rec

    def upsert(self, records: list[dict], now: str) -> dict:
        """返回 {'new': n, 'updated': n, 'unchanged': n}。"""
        stats = {"new": 0, "updated": 0, "unchanged": 0}
        with self.conn:
            for rec in records:
                row = self._to_row(rec)
                old = self.conn.execute("SELECT * FROM records WHERE id = ?", (rec["id"],)).fetchone()
                if old is None:
                    stats["new"] += 1
                    cols = list(FIELDS) + ["first_seen_at", "updated_at"]
                    vals = [row[f] for f in FIELDS] + [now, now]
                    self.conn.execute(
                        f"INSERT INTO records ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
                    )
                    # 删过又回来的条目：删除标记作废（新记录 updated_at 更新，下游按新记录覆盖）
                    self.conn.execute("DELETE FROM tombstones WHERE id = ?", (rec["id"],))
                    continue
                # 已有的非空字段不被空值覆盖（例如某次 feed 少了摘要）；
                # enrich 写入过的字段也不被 feed 的值覆盖（例如标题提取的关键词顶掉原文关键词）
                locked = set(json.loads(old["enriched_fields"] or "[]"))
                merged = {
                    f: old[f] if f in locked or row[f] in (None, "", "[]") else row[f]
                    for f in CONTENT_FIELDS
                }
                changed = any(old[f] != merged[f] for f in CONTENT_FIELDS)
                stats["updated" if changed else "unchanged"] += 1
                sets = ", ".join(f"{f} = ?" for f in CONTENT_FIELDS)
                self.conn.execute(
                    f"UPDATE records SET {sets}, fetched_at = ?, status = 'ok', updated_at = ? WHERE id = ?",
                    [merged[f] for f in CONTENT_FIELDS] + [row["fetched_at"], now if changed else old["updated_at"], rec["id"]],
                )
        return stats

    def mark_source_ok(self, source: str, now: str, count: int, warnings: list[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE records SET status = 'ok' WHERE source = ? AND status != 'ok'", (source,))
            self.conn.execute(
                """INSERT INTO source_runs(source, last_attempt_at, last_success_at, last_status, last_error, last_count, warnings)
                   VALUES (?, ?, ?, 'ok', NULL, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                     last_success_at=excluded.last_success_at, last_status='ok', last_error=NULL,
                     last_count=excluded.last_count, warnings=excluded.warnings""",
                (source, now, now, count, json.dumps(warnings, ensure_ascii=False) if warnings else None),
            )

    def mark_source_failed(self, source: str, now: str, error: str) -> int:
        """抓取失败：保留该来源已有数据，全部标为 stale；返回受影响条数。"""
        with self.conn:
            cur = self.conn.execute("UPDATE records SET status = 'stale' WHERE source = ?", (source,))
            self.conn.execute(
                """INSERT INTO source_runs(source, last_attempt_at, last_status, last_error)
                   VALUES (?, ?, 'failed', ?)
                   ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
                     last_status='failed', last_error=excluded.last_error""",
                (source, now, error[:1000]),
            )
            return cur.rowcount

    def _tombstone(self, rec_id: str, source: str, now: str, reason: str) -> None:
        self.conn.execute(
            """INSERT INTO tombstones(id, source, deleted_at, reason) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET source=excluded.source, deleted_at=excluded.deleted_at, reason=excluded.reason""",
            (rec_id, source, now, reason),
        )

    def purge_source(self, source: str, now: str | None = None) -> int:
        """删除已停用来源的全部记录和运行状态；返回删除条数。被删的每条留删除标记（reason=retired）。"""
        from .util import now_iso
        now = now or now_iso()
        with self.conn:
            for row in self.conn.execute("SELECT id FROM records WHERE source = ?", (source,)).fetchall():
                self._tombstone(row["id"], source, now, "retired")
            cur = self.conn.execute("DELETE FROM records WHERE source = ?", (source,))
            self.conn.execute("DELETE FROM source_runs WHERE source = ?", (source,))
            self.conn.execute("DELETE FROM enrich_attempts WHERE id NOT IN (SELECT id FROM records)")
            return cur.rowcount

    def backfill_dois(self, sources: list[str], derive, now: str | None = None) -> dict:
        """给没有 DOI、但现在能从 URL 推出 DOI 的旧记录补上 DOI，并把 id 换成按 DOI 计算的 id。
        若按 DOI 的新记录已存在（feed 已重新抓到），删掉旧的那条。返回 {'rekeyed': n, 'merged': n}。"""
        from .util import make_id, now_iso
        now = now or now_iso()
        out = {"rekeyed": 0, "merged": 0}
        if not sources:
            return out
        marks = ",".join("?" * len(sources))
        rows = self.conn.execute(
            f"SELECT id, url, source FROM records WHERE doi IS NULL AND kind = 'article' AND source IN ({marks})", sources
        ).fetchall()
        with self.conn:
            for row in rows:
                doi = derive(row["url"])
                if not doi:
                    continue
                new_id = make_id(doi, row["url"])
                if self.conn.execute("SELECT 1 FROM records WHERE id = ?", (new_id,)).fetchone():
                    self.conn.execute("DELETE FROM records WHERE id = ?", (row["id"],))
                    self._tombstone(row["id"], row["source"], now, "merged")
                    out["merged"] += 1
                else:
                    # 换 id 等于「旧 id 删除 + 新 id 出现」：旧 id 留删除标记，新 id 的 updated_at 更新，下游才会收到
                    self.conn.execute("UPDATE records SET id = ?, doi = ?, updated_at = ? WHERE id = ?",
                                      (new_id, doi, now, row["id"]))
                    self.conn.execute("DELETE FROM tombstones WHERE id = ?", (new_id,))
                    self._tombstone(row["id"], row["source"], now, "rekeyed")
                    out["rekeyed"] += 1
                self.conn.execute("DELETE FROM enrich_attempts WHERE id = ?", (row["id"],))
        return out

    # ---- enrich（按 DOI 回查） ----

    def enrich_candidates(self, sources: list[str], since: str, retry_before: str, limit: int) -> list[dict]:
        """待回查：指定来源的 kind=article，有 DOI，摘要或许可为空，发布（或首次见到）不早于 since，
        且从没试过或上次尝试早于 retry_before。从没试过的优先，其次按发布时间从新到旧。"""
        if not sources or limit <= 0:
            return []
        marks = ",".join("?" * len(sources))
        sql = f"""
            SELECT r.*, a.attempts AS _attempts, a.last_attempt_at AS _last_attempt_at
            FROM records r LEFT JOIN enrich_attempts a ON a.id = r.id
            WHERE r.kind = 'article' AND r.doi IS NOT NULL AND r.source IN ({marks})
              AND (r.summary IS NULL OR r.summary = '' OR r.license IS NULL OR r.license = '')
              AND COALESCE(r.published_at, r.first_seen_at) >= ?
              AND (a.last_attempt_at IS NULL OR a.last_attempt_at < ?)
            ORDER BY (a.id IS NOT NULL), COALESCE(r.published_at, r.first_seen_at) DESC, r.id
            LIMIT ?"""
        out = []
        for row in self.conn.execute(sql, [*sources, since, retry_before, limit]):
            rec = self._from_row(row)
            rec["_enriched_fields"] = json.loads(row["enriched_fields"] or "[]")
            out.append(rec)
        return out

    def apply_enrichment(self, rec_id: str, updates: dict, now: str, source: str) -> list[str]:
        """只写 updates 里的字段（调用方已保证原值为空或可替换），记下 enriched_at / enrich_source。
        返回实际写入的字段名。"""
        if not updates:
            return []
        old = self.conn.execute("SELECT * FROM records WHERE id = ?", (rec_id,)).fetchone()
        if old is None:
            return []
        fields = json.loads(old["enriched_fields"] or "[]")
        row = self._to_row({**self._from_row(old), **updates})
        for f in updates:
            if f not in fields:
                fields.append(f)
        sets = ", ".join(f"{f} = ?" for f in updates)
        with self.conn:
            self.conn.execute(
                f"UPDATE records SET {sets}, enriched_at = ?, enrich_source = ?, enriched_fields = ?, updated_at = ? WHERE id = ?",
                [row[f] for f in updates] + [now, source, json.dumps(fields), now, rec_id],
            )
        return list(updates)

    def record_attempt(self, rec_id: str, now: str, result: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO enrich_attempts(id, attempts, first_attempt_at, last_attempt_at, last_result, last_error)
                   VALUES (?, 1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET attempts = attempts + 1, last_attempt_at = excluded.last_attempt_at,
                     last_result = excluded.last_result, last_error = excluded.last_error""",
                (rec_id, now, now, result, (error or None) and error[:500]),
            )

    def enrich_summary(self) -> dict:
        r = self.conn.execute(
            """SELECT COUNT(*) n, SUM(last_result IN ('filled','partial','found_nothing_new')) found,
                      SUM(last_result = 'not_found') not_found, SUM(last_result = 'error') errors
               FROM enrich_attempts"""
        ).fetchone()
        return {"tracked": r["n"], "found": r["found"] or 0, "not_found": r["not_found"] or 0, "errors": r["errors"] or 0}

    def iter_records(self, since: str | None = None, source: str | None = None):
        """逐行读出（游标迭代，不整表进内存）。"""
        sql, args, where = "SELECT * FROM records", [], []
        if since:
            where.append("COALESCE(published_at, first_seen_at) >= ?")
            args.append(since)
        if source:
            where.append("source = ?")
            args.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(published_at, first_seen_at) DESC, source, id"
        for r in self.conn.execute(sql, args):
            yield self._from_row(r)

    # ---- 下游推送（push.py）：按 (updated_at, id) / (deleted_at, id) 复合游标增量读 ----

    def changed_since(self, cursor: dict | None, since: str, limit: int,
                      sources: list[str] | None = None) -> list[dict]:
        """updated_at+id 严格大于游标、且在窗口内（COALESCE(published_at, first_seen_at) >= since）的记录，按游标顺序。
        每条另带 hs_updated_at（游标用）和 sort_key（= 导出排序键）。sources 非空时只返回这些 source。"""
        sql = "SELECT * FROM records WHERE COALESCE(published_at, first_seen_at) >= ?"
        args: list = [since]
        if sources:
            sql += " AND source IN (%s)" % ",".join("?" * len(sources))
            args += list(sources)
        if cursor:
            sql += " AND (updated_at > ? OR (updated_at = ? AND id > ?))"
            args += [cursor["updatedAt"], cursor["updatedAt"], cursor["id"]]
        sql += " ORDER BY updated_at, id LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(sql, args):
            rec = self._from_row(r)
            rec["hs_updated_at"] = r["updated_at"]
            rec["sort_key"] = r["published_at"] or r["first_seen_at"]
            out.append(rec)
        return out

    def tombstones_since(self, cursor: dict | None, limit: int,
                         sources: list[str] | None = None) -> list[dict]:
        if not self._has_table("tombstones"):
            return []
        where, args = [], []
        if sources:
            where.append("source IN (%s)" % ",".join("?" * len(sources)))
            args += list(sources)
        if cursor:
            where.append("(deleted_at > ? OR (deleted_at = ? AND id > ?))")
            args += [cursor["deletedAt"], cursor["deletedAt"], cursor["id"]]
        sql = "SELECT * FROM tombstones"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY deleted_at, id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def window_records(self, since: str, sources: list[str] | None = None):
        """对账用：窗口内全部记录（游标迭代）。sources 非空时只含这些 source。"""
        sql = "SELECT * FROM records WHERE COALESCE(published_at, first_seen_at) >= ?"
        args: list = [since]
        if sources:
            sql += " AND source IN (%s)" % ",".join("?" * len(sources))
            args += list(sources)
        sql += " ORDER BY id"
        for r in self.conn.execute(sql, args):
            rec = self._from_row(r)
            rec["hs_updated_at"] = r["updated_at"]
            rec["sort_key"] = r["published_at"] or r["first_seen_at"]
            yield rec

    def _has_table(self, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def sources_present(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT source FROM records ORDER BY source")]

    def records(self, since: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records"
        args: list = []
        if since:
            sql += " WHERE COALESCE(published_at, first_seen_at) >= ?"
            args.append(since)
        sql += " ORDER BY COALESCE(published_at, first_seen_at) DESC, source, id"
        return [self._from_row(r) for r in self.conn.execute(sql, args)]

    def source_runs(self) -> dict[str, dict]:
        return {r["source"]: dict(r) for r in self.conn.execute("SELECT * FROM source_runs ORDER BY source")}

    def source_summary(self) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute(
            """SELECT source, COUNT(*) n, MAX(published_at) latest, SUM(status='stale') stale,
                      SUM(summary IS NOT NULL AND summary != '') with_summary,
                      SUM(enriched_at IS NOT NULL) enriched,
                      SUM(COALESCE(enriched_fields, '') LIKE '%"summary"%') summary_enriched
               FROM records GROUP BY source"""
        ):
            out[r["source"]] = {"records": r["n"], "latest_published_at": r["latest"], "stale": r["stale"],
                                "with_summary": r["with_summary"], "enriched": r["enriched"],
                                "summary_from_enrich": r["summary_enriched"]}
        return out
