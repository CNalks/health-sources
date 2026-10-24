"""通用 RSS/Atom 适配器：Eurosurveillance、EID、ECDC、PLOS NTDs。"""
from __future__ import annotations

import re

import feedparser

from ..util import doi_from_url, struct_time_to_iso
from .base import make_record


def _summary(entry) -> str | None:
    text = entry.get("summary") or ""
    if not text and entry.get("content"):
        text = entry["content"][0].get("value", "")
    # PLOS 摘要第一段是作者列表 "<p>by A, B</p>"；EID 摘要只有 "X et al."，都去掉
    text = re.sub(r"^\s*<p>\s*by [^<]*</p>", "", text)
    if re.fullmatch(r"\s*[^<]{0,80}\bet al\.?\s*", text):
        return None
    return text


def _native_keywords(entry) -> list[str]:
    return [t.get("term") for t in entry.get("tags") or [] if t.get("term")]


def parse(payload: bytes, cfg: dict, fetched_at: str, feed_category: str | None = None) -> list[dict]:
    feed = feedparser.parse(payload)
    if feed.bozo and not feed.entries:
        raise ValueError(f"feed parse error: {feed.bozo_exception!r}")
    records = []
    for entry in feed.entries[: cfg.get("max_items", 50)]:
        url = entry.get("link") or entry.get("id")
        title = entry.get("title")
        if not url or not title:
            continue
        doi = None
        entry_id = entry.get("id") or ""
        if entry_id.startswith("10."):
            doi = entry_id
        elif cfg.get("doi_from_url", True):
            doi = doi_from_url(url)
        published = struct_time_to_iso(entry.get("published_parsed") or entry.get("updated_parsed"))
        native = _native_keywords(entry)
        rec = make_record(
            cfg,
            title=title,
            url=url,
            published_at=published,
            fetched_at=fetched_at,
            doi=doi,
            summary=_summary(entry),
            native_keywords=native,
        )
        if feed_category:
            # 子 feed 的栏目名（如 ECDC 的 Risk assessment）作为 RSS category 放在首位
            rest = [k for k in rec["keywords"] if k.lower() != feed_category.lower()]
            rec["keywords"] = [feed_category] + rest[:5]
        records.append(rec)
    return records


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    feeds = cfg.get("feeds") or [{"url": cfg["url"]}]
    records: list[dict] = []
    errors: list[str] = []
    for f in feeds:
        try:
            payload = http.get(f["url"]).content
            records.extend(parse(payload, cfg, fetched_at, f.get("category")))
        except Exception as exc:  # 单个子 feed 失败不影响同来源其他 feed
            errors.append(f"{f['url']}: {exc}")
    if errors and not records:
        raise RuntimeError("; ".join(errors))
    if errors:
        cfg.setdefault("_warnings", []).extend(errors)
    return records
