"""采集主流程：清理已停用来源 → 逐个来源抓取 → 校验 → 写 SQLite → 按 DOI 回查补全 → 导出当日快照。
任何一个来源失败都不影响其他来源：失败来源沿用上次数据并标为 stale。补全步骤失败也不影响采集和导出。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import enrich as enrich_mod
from .adapters import ADAPTERS
from .http import Http
from . import tags as tagmod
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
    retired = cfg.get("retired_sources") or []
    if ids & set(retired):
        raise ValueError(f"source listed as both active and retired: {sorted(ids & set(retired))}")
    return cfg


def purge_retired(cfg: dict, store: Store, now: str | None = None) -> dict[str, int]:
    """已停用来源（sources.yaml 的 retired_sources）：删除库里的记录，不再导出。"""
    out = {}
    for sid in cfg.get("retired_sources") or []:
        n = store.purge_source(sid, now)
        if n:
            log.info("retired source %s: deleted %d records", sid, n)
        out[sid] = n
    return out


def backfill_dois(cfg: dict, store: Store, now: str | None = None) -> dict:
    """旧记录（如 v0.1 时 MMWR 提前发布 / 增刊没推出 DOI）补 DOI，避免同一篇按 URL、按 DOI 各算一条。"""
    from .util import doi_from_url
    ids = [s["id"] for s in cfg["sources"] if s["kind"] == "article" and s.get("doi_from_url", True)
           and s.get("id_from") != "url"]
    out = store.backfill_dois(ids, doi_from_url, now)
    if out["rekeyed"] or out["merged"]:
        log.info("backfilled DOIs from URL: %s", out)
    return out


def run_enrich(cfg: dict, store: Store, now: str, http: Http | None = None, only: list[str] | None = None,
               overrides: dict | None = None) -> dict:
    conf = {**enrich_mod.DEFAULTS, **(cfg.get("enrich") or {}), **(overrides or {})}
    d = cfg.get("defaults", {})
    http = http or Http(d.get("user_agent", "health-sources/0.1"), conf.get("timeout", 20), conf.get("retries", 1))
    sources = {s["id"]: s for s in cfg["sources"] if not only or s["id"] in only}
    try:
        return enrich_mod.run(store, http, conf, sources, now)
    except Exception as exc:  # noqa: BLE001 —— 补全失败不影响主流程
        log.warning("enrich step FAILED: %s: %s", type(exc).__name__, exc)
        return {"enabled": True, "error": f"{type(exc).__name__}: {exc}"}


def collect_source(src: dict, http: Http, store: Store, now: str) -> dict:
    adapter = ADAPTERS[src["adapter"]]
    try:
        raw = adapter.fetch(src, http, now)
        good, bad = [], []
        for rec in raw:
            rec.setdefault("disease_tags", [])
            rec.setdefault("country_iso3", [])
            tagmod.apply_tags(rec)
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
            if src.get("allow_empty") and len(bad) == 0:
                warnings = list(src.pop("_warnings", []))
                store.mark_source_ok(src["id"], now, 0, warnings)
                log.info("%s ok: 0 records (empty feed allowed)", src["id"])
                return {"source": src["id"], "ok": True, "count": 0, "invalid": 0,
                        "latest": None, "warnings": warnings, "new": 0, "updated": 0, "unchanged": 0}
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


def _atomic_write_lines(path: Path, lines) -> tuple[int, int]:
    """逐行写临时文件再原子替换；返回 (行数, 字节数)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    n = size = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line)
                n += 1
        os.chmod(tmp, 0o644)
        size = os.path.getsize(tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return n, size


def _jsonl(records):
    for r in records:
        yield json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n"


def export(store: Store, data_dir: Path, window_days: int = 30, keep_days: int = 90, results: list | None = None,
           enrich_stats: dict | None = None, retired: dict | None = None, by_source_days: int = 400) -> dict:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=window_days)).isoformat()
    day = now.astimezone(BEIJING).date().isoformat()
    exports = data_dir / "exports"
    total, _ = _atomic_write_lines(exports / f"{day}.jsonl", _jsonl(store.iter_records(since=since)))
    _atomic_write_lines(data_dir / "latest.jsonl", _jsonl(store.iter_records(since=since)))
    # 按来源的长窗口导出：按期组织的下游（如公卫前沿取 CDC Weekly 卷期篇目）读这里
    bs_since = (now - timedelta(days=by_source_days)).isoformat()
    by_source = {}
    present = store.sources_present()
    for sid in present:
        n, size = _atomic_write_lines(data_dir / "by-source" / f"{sid}.jsonl",
                                      _jsonl(store.iter_records(since=bs_since, source=sid)))
        by_source[sid] = {"records": n, "bytes": size, "path": f"by-source/{sid}.jsonl"}
    for f in (data_dir / "by-source").glob("*.jsonl"):
        if f.stem not in present:
            f.unlink()
    status = {
        "generated_at": now.replace(microsecond=0).isoformat(),
        "window_days": window_days,
        "by_source_days": by_source_days,
        "total": total,
        "sources": store.source_summary(),
        "runs": store.source_runs(),
        "by_source": by_source,
        "last_run": results or [],
        "last_enrich": enrich_stats or {},
        "enrich_attempts": store.enrich_summary(),
        "retired_sources": retired or {},
    }
    _atomic_write(data_dir / "status.json", json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    cutoff = (now - timedelta(days=keep_days)).astimezone(BEIJING).date().isoformat()
    for f in exports.glob("*.jsonl"):
        if f.stem < cutoff:
            f.unlink()
    return status


def export_opts(cfg: dict) -> dict:
    e = cfg.get("export") or {}
    return {"window_days": int(e.get("window_days", 30)), "by_source_days": int(e.get("by_source_days", 400))}


def run(config_path: str | Path, data_dir: str | Path, only: list[str] | None = None,
        do_enrich: bool = True, enrich_http: Http | None = None) -> list[dict]:
    cfg = load_config(config_path)
    data_dir = Path(data_dir)
    d = cfg.get("defaults", {})
    http = Http(d.get("user_agent", "health-sources/0.1"), d.get("timeout", 40), d.get("retries", 2))
    store = Store(data_dir / "health_sources.sqlite")
    now = now_iso()
    results = []
    try:
        retired = purge_retired(cfg, store, now)
        backfill_dois(cfg, store, now)
        for src in cfg["sources"]:
            if only and src["id"] not in only:
                continue
            results.append(collect_source(src, http, store, now))
        enrich_stats = run_enrich(cfg, store, now, enrich_http, only) if do_enrich else {"enabled": False}
        export(store, data_dir, results=results, enrich_stats=enrich_stats, retired=retired, **export_opts(cfg))
    finally:
        store.close()
    return results


def run_backfill(config_path: str | Path, data_dir: str | Path, sources: list[str], since: str,
                 opts: dict | None = None, http: Http | None = None, sleep=None) -> list[dict]:
    """往期回补指定来源（见 health_sources/backfill.py），之后重新导出。不改 source_runs。"""
    from . import backfill as bf
    cfg = load_config(config_path)
    data_dir = Path(data_dir)
    d = cfg.get("defaults", {})
    http = http or Http(d.get("user_agent", "health-sources/0.1"), d.get("timeout", 40), d.get("retries", 2))
    store = Store(data_dir / "health_sources.sqlite")
    now = now_iso()
    out = []
    try:
        for src in cfg["sources"]:
            if sources and src["id"] not in sources:
                continue
            kw = {"sleep": sleep} if sleep else {}
            res = bf.backfill_source(src, store, http, now, since, opts, **kw)
            bf.write_report(data_dir, res)
            out.append(res)
        export(store, data_dir, **export_opts(cfg))
    finally:
        store.close()
    return out
