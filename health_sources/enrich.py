"""补全步骤：按 DOI 回查 Europe PMC，给缺摘要 / 缺许可的文章补上摘要、许可、PMCID 和关键词。

规则（README「补全」一节有同样说明）：
- 只处理 kind=article、有 DOI、摘要或许可为空的记录；
- 只在原字段为空时写入，已有值不覆盖，source 不改；关键词例外：原来只是从标题自动提取的，
  才换成 Europe PMC 的原文关键词（自动提取的不算「已有值」）；
- 来源配置了 figures_allowed: per_record 时，按单篇许可重算 figures_allowed，只有 CC0 / CC BY / CC BY-SA 允许；
- 查不到或仍缺摘要：记一次尝试，隔天再试，发布后 30 天内反复尝试（Europe PMC 收录可能晚 10 天以上）；
- 每次运行最多查 max_per_run 篇，请求之间 sleep；任何错误只记日志，不影响采集和导出。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from . import keywords as kw
from .util import clean_summary, normalize_doi

log = logging.getLogger("health_sources.enrich")

DEFAULTS = {
    "url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
    "max_per_run": 60,
    "delay_seconds": 1.0,
    "window_days": 30,
    "retry_after_hours": 20,   # 定时器每天跑一次，用 20 小时避免「差几秒不满 24 小时」被跳过
    "max_seconds": 300,        # 整个步骤的时间预算
    "figures_allowed_licenses": ["cc0", "cc by", "cc by-sa"],
}
SOURCE_NAME = "europepmc"
_GOOD_SRC = {"MED": 0, "PMC": 1}   # 优先正式出版记录，预印本（PPR）等排后


def figures_allowed(license_str: str | None, allowed: list[str]) -> bool:
    if not license_str:
        return False
    return license_str.strip().lower() in {a.lower() for a in allowed}


def build_params(doi: str) -> dict:
    return {"query": f'DOI:"{doi}"', "format": "json", "resultType": "core", "pageSize": "5"}


def merge_results(payload: bytes, doi: str) -> dict | None:
    """把同一 DOI 的多条结果（MED + PMC 常各一条，PMC 那条有时不带 doi 字段）合并成一份。
    返回 {'summary','license','pmcid','keywords'}（缺的为 None / []）；没有结果返回 None。"""
    data = json.loads(payload)
    if "resultList" not in data:
        raise ValueError(f"unexpected response: {str(data)[:200]}")
    hits = []
    for r in data["resultList"].get("result", []):
        rdoi = normalize_doi(r.get("doi"))
        if rdoi and rdoi != doi:
            continue  # 防御：检索式误匹配到别的 DOI
        hits.append(r)
    if not hits:
        return None
    hits.sort(key=lambda r: _GOOD_SRC.get(r.get("source"), 9))
    out = {"summary": None, "license": None, "pmcid": None, "keywords": []}
    for r in hits:
        if not out["summary"]:
            out["summary"] = clean_summary(r.get("abstractText"))
        if not out["license"] and r.get("license"):
            out["license"] = r["license"].strip().upper()
        if not out["pmcid"] and r.get("pmcid"):
            out["pmcid"] = r["pmcid"].strip().upper()
        if not out["keywords"]:
            out["keywords"] = [k for k in ((r.get("keywordList") or {}).get("keyword") or []) if k]
    return out


def plan_updates(rec: dict, found: dict, src_cfg: dict, allowed: list[str]) -> dict:
    """算出要写的字段：只填空字段。"""
    up: dict = {}
    if not rec.get("summary") and found["summary"]:
        up["summary"] = found["summary"]
    if not rec.get("license") and found["license"]:
        up["license"] = found["license"]
    if not rec.get("pmcid") and found["pmcid"]:
        up["pmcid"] = found["pmcid"]
    if found["keywords"]:
        auto = kw.pick(None, rec["title"], src_cfg.get("ignore_categories"))
        if not rec["keywords"] or rec["keywords"] == auto:
            native = kw.pick(found["keywords"], rec["title"], src_cfg.get("ignore_categories"))
            if native != rec["keywords"]:
                up["keywords"] = native
    if src_cfg.get("figures_allowed") == "per_record":
        lic = up.get("license") or rec.get("license")
        fa = figures_allowed(lic, src_cfg.get("figures_allowed_licenses") or allowed)
        if fa != rec["figures_allowed"]:
            up["figures_allowed"] = fa
    return up


def run(store, http, cfg: dict, sources: dict[str, dict], now: str, sleep=time.sleep) -> dict:
    """返回统计；本函数不抛异常（调用方另有兜底）。"""
    conf = {**DEFAULTS, **(cfg or {})}
    stats = {"enabled": bool(conf.get("enabled", True)), "attempted": 0, "found": 0, "not_found": 0,
             "errors": 0, "filled": 0, "summary_filled": 0, "license_filled": 0, "pmcid_filled": 0,
             "keywords_filled": 0, "figures_changed": 0, "candidates": 0, "stopped_early": False}
    if not stats["enabled"]:
        return stats
    article_sources = [sid for sid, s in sources.items() if s.get("kind") == "article" and s.get("enrich", True)]
    now_dt = datetime.fromisoformat(now)
    since = (now_dt - timedelta(days=int(conf["window_days"]))).isoformat()
    retry_before = (now_dt - timedelta(hours=float(conf["retry_after_hours"]))).isoformat()
    cands = store.enrich_candidates(article_sources, since, retry_before, int(conf["max_per_run"]))
    stats["candidates"] = len(cands)
    started = time.monotonic()
    for i, rec in enumerate(cands):
        if time.monotonic() - started > float(conf["max_seconds"]):
            stats["stopped_early"] = True
            log.warning("enrich: time budget used up after %d lookups", i)
            break
        if i:
            sleep(float(conf["delay_seconds"]))
        stats["attempted"] += 1
        try:
            resp = http.get(conf["url"], params=build_params(rec["doi"]))
            found = merge_results(resp.content, rec["doi"])
        except Exception as exc:  # noqa: BLE001 —— 单篇失败只记一次尝试
            stats["errors"] += 1
            store.record_attempt(rec["id"], now, "error", f"{type(exc).__name__}: {exc}")
            log.warning("enrich %s (%s) error: %s", rec["doi"], rec["source"], exc)
            continue
        if found is None:
            stats["not_found"] += 1
            store.record_attempt(rec["id"], now, "not_found")
            continue
        stats["found"] += 1
        up = plan_updates(rec, found, sources.get(rec["source"], {}), conf["figures_allowed_licenses"])
        written = store.apply_enrichment(rec["id"], up, now, SOURCE_NAME)
        for f in written:
            key = {"figures_allowed": "figures_changed"}.get(f, f"{f}_filled")
            stats[key] = stats.get(key, 0) + 1
        if written:
            stats["filled"] += 1
        still_missing = not (rec.get("summary") or up.get("summary")) or not (rec.get("license") or up.get("license"))
        result = ("partial" if still_missing else "filled") if written else "found_nothing_new"
        store.record_attempt(rec["id"], now, result)
    log.info("enrich: %d candidates, %d looked up, %d found, %d not found, %d errors, %d summaries filled",
             stats["candidates"], stats["attempted"], stats["found"], stats["not_found"], stats["errors"],
             stats["summary_filled"])
    return stats
