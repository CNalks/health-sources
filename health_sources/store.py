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
    updated_at TEXT NOT NULL
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
"""

CONTENT_FIELDS = [f for f in FIELDS if f not in ("id", "fetched_at", "status")]


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _to_row(rec: dict) -> dict:
        row = dict(rec)
        row["keywords"] = json.dumps(rec["keywords"], ensure_ascii=False)
        row["figures_allowed"] = int(bool(rec["figures_allowed"]))
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row) -> dict:
        rec = {f: row[f] for f in FIELDS}
        rec["keywords"] = json.loads(rec["keywords"])
        rec["figures_allowed"] = bool(rec["figures_allowed"])
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
                    continue
                changed = any(old[f] != row[f] for f in CONTENT_FIELDS)
                # 已有的非空字段不被空值覆盖（例如某次 feed 少了摘要）
                merged = {f: (row[f] if row[f] not in (None, "", "[]") else old[f]) for f in CONTENT_FIELDS}
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
            "SELECT source, COUNT(*) n, MAX(published_at) latest, SUM(status='stale') stale FROM records GROUP BY source"
        ):
            out[r["source"]] = {"records": r["n"], "latest_published_at": r["latest"], "stale": r["stale"]}
        return out
