"""Europe PMC REST search：最近 N 天首发、开放获取的公卫/传染病文章。
license 与 figures_allowed 按每篇文章自己的许可判断。"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..util import parse_iso
from .base import make_record

LANG_MAP = {"eng": "en", "chi": "zh", "zho": "zh", "fre": "fr", "fra": "fr", "spa": "es", "por": "pt", "ger": "de", "deu": "de"}


def build_query(cfg: dict, today: date | None = None) -> str:
    today = today or date.today()
    start = today - timedelta(days=int(cfg.get("days", 7)))
    return f"({cfg['query']}) AND FIRST_PDATE:[{start.isoformat()} TO {today.isoformat()}]"


def figures_allowed(license_str: str | None, cfg: dict) -> bool:
    if not license_str:
        return False
    lic = license_str.strip().lower()
    allowed = [a.lower() for a in cfg.get("figures_allowed_licenses", ["cc0", "cc by"])]
    return lic in allowed


def parse(payload: bytes, cfg: dict, fetched_at: str) -> list[dict]:
    data = json.loads(payload)
    if "resultList" not in data:
        raise ValueError(f"unexpected response: {str(data)[:200]}")
    records = []
    for r in data["resultList"].get("result", []):
        title = r.get("title")
        if not title:
            continue
        doi = r.get("doi")
        src, rid = r.get("source"), r.get("id")
        pmcid = r.get("pmcid")
        url = (f"https://europepmc.org/article/PMC/{pmcid}" if pmcid
               else f"https://europepmc.org/article/{src}/{rid}")
        native = (r.get("keywordList") or {}).get("keyword") or []
        lic = r.get("license")
        pub = r.get("firstPublicationDate") or r.get("electronicPublicationDate")
        records.append(
            make_record(
                cfg,
                title=title.rstrip("."),
                url=url,
                published_at=parse_iso(pub + "T00:00:00+00:00") if pub else None,
                fetched_at=fetched_at,
                doi=doi,
                summary=r.get("abstractText"),
                native_keywords=native,
                license=lic.upper() if lic else None,
                figures_allowed=figures_allowed(lic, cfg),
                lang=LANG_MAP.get((r.get("language") or "eng").lower(), "en"),
            )
        )
    return records


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    params = {
        "query": build_query(cfg),
        "format": "json",
        "resultType": "core",
        "pageSize": str(cfg.get("page_size", 100)),
        "sort": "P_PDATE_D desc",
    }
    return parse(http.get(cfg["url"], params=params).content, cfg, fetched_at)
