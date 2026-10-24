"""适配器共用的记录构造函数。"""
from __future__ import annotations

from .. import keywords as kw
from ..util import clean_summary, make_id, normalize_doi, normalize_url, strip_html


def _is_boilerplate(summary: str | None, cfg: dict) -> bool:
    s = strip_html(summary)
    return any(s == strip_html(b) for b in cfg.get("ignore_summaries", []))


def make_record(
    cfg: dict,
    *,
    title: str,
    url: str,
    published_at: str | None,
    fetched_at: str,
    doi: str | None = None,
    summary: str | None = None,
    native_keywords: list[str] | None = None,
    license: str | None = None,
    figures_allowed: bool | None = None,
    lang: str | None = None,
) -> dict:
    # license 为 per_record 的来源（Europe PMC）只认每篇自己的许可，不回落到来源默认值
    per_record = cfg.get("license") == "per_record"
    doi = normalize_doi(doi)
    url = normalize_url(url)
    title = strip_html(title)
    return {
        # id_from: url 的来源（Europe PMC）用规范化 URL 做 id：同一篇先以 PMC 收录、
        # 几天后才补上 DOI，用 URL 才不会变成两条
        "id": make_id(None if cfg.get("id_from") == "url" else doi, url),
        "source": cfg["id"],
        "kind": cfg["kind"],
        "title": title,
        "lang": lang or cfg.get("lang", "en"),
        "published_at": published_at,
        "url": url,
        "doi": doi,
        "summary": None if _is_boilerplate(summary, cfg) else clean_summary(summary),
        "keywords": kw.pick(native_keywords, title, cfg.get("ignore_categories")),
        "license": license if per_record or license is not None else cfg.get("license"),
        "figures_allowed": bool(figures_allowed) if per_record or figures_allowed is not None
        else bool(cfg.get("figures_allowed", False)),
        "fetched_at": fetched_at,
        "status": "ok",
    }
