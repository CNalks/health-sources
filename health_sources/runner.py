"""采集主流程：逐个来源抓取 → 校验 → 写 SQLite → 导出当日快照。
任何一个来源失败都不影响其他来源：失败来源沿用上次数据并标为 stale。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .adapters import ADAPTERS
from .http import Http
from .schema import validate
from .store import Store
from .util import now_iso

log = logging.getLogger("health_sources")
BEIJING = timezone(timedelta(hours=8))


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    defaults = cfg.get("defaults", {})
    ids = set()
    for src in cfg["sources"]:
        if src["id"] in ids:
            raise ValueError(f"duplicate source id {src['id']}")
        ids.add(src["id"])
        if src["adapter"] not in ADAPTERS:
            raise ValueError(f"unknown adapter {src['adapter']} for {src['id']}")
        if src["kind"] not in ("article", "news"):
            raise ValueError(f"bad kind for {src['id']}")
        for k, v in defaults.items():
            src.setdefault(k, v)
    return cfg


def collect_source(src: dict, http: Http, store: Store, now: str) -> dict:
    adapter = ADAPTERS[src["adapter"]]
    try:
        raw = adapter.fetch(src, http, now)
        good, bad = [], []
        for rec in raw:
            errs = validate(rec)
            (bad if errs else good).append((rec, errs))
        records = [r for r, _ in good]
        # 同一批里重复 id 只保留第一条
        seen, deduped = set(), []
        for r in records:
            if r["id"] not in seen:
                seen.add(r["id"])
                deduped.append(r)
        if not deduped:
            raise RuntimeError(f"no valid records (raw={len(raw)}, invalid={len(bad)})")
        warnings = list(src.pop("_warnings", []))
        warnings += [f"invalid record {r.get('url')}: {e}" for r, e in bad[:5]]
        stats = store.upsert(deduped, now)
        store.mark_source_ok(src["id"], now, len(deduped), warnings)
        latest = max((r["published_at"] for r in deduped if r["published_at"]), default=None)
        log.info("%s ok: %d records (new %d, updated %d), invalid %d, latest %s",
                 src["id"], len(deduped), stats["new"], stats["updated"], len(bad), latest)
        return {"source": src["id"], "ok": True, "count": len(deduped), "invalid": len(bad),
                "latest": latest, "warnings": warnings, **stats}
    except Exception as exc:  # noqa: BLE001 —— 隔离单个来源的任何错误
        src.pop("_warnings", None)
        msg = f"{type(exc).__name__}: {exc}"
        n = store.mark_source_failed(src["id"], now, msg)
        log.warning("%s FAILED (%s); kept %d previous records as stale", src["id"], msg, n)
        return {"source": src["id"], "ok": False, "error": msg, "stale": n}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def export(store: Store, data_dir: Path, window_days: int = 30, keep_days: int = 90, results: list | None = None) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=window_days)).isoformat()
    records = store.records(since=since)
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n" for r in records)
    day = now.astimezone(BEIJING).date().isoformat()
    exports = data_dir / "exports"
    _atomic_write(exports / f"{day}.jsonl", body)
    _atomic_write(data_dir / "latest.jsonl", body)
    status = {
        "generated_at": now.replace(microsecond=0).isoformat(),
        "window_days": window_days,
        "total": len(records),
        "sources": store.source_summary(),
        "runs": store.source_runs(),
        "last_run": results or [],
    }
    _atomic_write(data_dir / "status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    cutoff = (now - timedelta(days=keep_days)).astimezone(BEIJING).date().isoformat()
    for f in exports.glob("*.jsonl"):
        if f.stem < cutoff:
            f.unlink()
    return status


def run(config_path: str | Path, data_dir: str | Path, only: list[str] | None = None) -> list[dict]:
    cfg = load_config(config_path)
    data_dir = Path(data_dir)
    d = cfg.get("defaults", {})
    http = Http(d.get("user_agent", "health-sources/0.1"), d.get("timeout", 40), d.get("retries", 2))
    store = Store(data_dir / "health_sources.sqlite")
    now = now_iso()
    results = []
    try:
        for src in cfg["sources"]:
            if only and src["id"] not in only:
                continue
            results.append(collect_source(src, http, store, now))
        export(store, data_dir, results=results)
    finally:
        store.close()
    return results
