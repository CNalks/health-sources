"""通用 RSS/Atom/RDF 适配器：Eurosurveillance、EID、ECDC、PLOS NTDs、CDC Weekly；亦可 inbox（如台 CDC）。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import feedparser

from ..util import doi_from_url, normalize_doi, struct_time_to_iso
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


def _entry_doi(entry) -> str | None:
    """条目自带的 DOI：PRISM <prism:doi>、<dc:identifier>doi:...</dc:identifier>，或 id 本身就是 DOI。"""
    for value in (entry.get("prism_doi"), entry.get("dc_identifier"), entry.get("id")):
        if not value:
            continue
        value = value.strip()
        if value.lower().startswith(("10.", "doi:", "https://doi.org/", "http://dx.doi.org/")):
            doi = normalize_doi(value)
            if doi:
                return doi
    return None


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
        base = cfg.get("url_origin") or cfg.get("link_base")
        if base and url.startswith("/"):
            url = base.rstrip("/") + url
        doi = _entry_doi(entry)
        if not doi and cfg.get("doi_from_url", True):
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
            # feed_summary: false —— feed 的描述不是摘要（如 CDC Weekly 只有卷期页码），丢弃
            summary=_summary(entry) if cfg.get("feed_summary", True) else None,
            native_keywords=native,
        )
        if feed_category:
            # 子 feed 的栏目名（如 ECDC 的 Risk assessment）作为 RSS category 放在首位
            rest = [k for k in rec["keywords"] if k.lower() != feed_category.lower()]
            rec["keywords"] = [feed_category] + rest[:5]
        records.append(rec)
    return records


def _from_inbox(path: Path, cfg: dict) -> list[dict]:
    age_h = (time.time() - path.stat().st_mtime) / 3600
    max_age = float(cfg.get("inbox_max_age_hours", 72))
    if age_h > max_age:
        raise RuntimeError(f"inbox {path.name} is {age_h:.0f} h old (> {max_age:.0f} h); last push from Actions missing")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("source") != cfg["id"]:
            raise ValueError(f"inbox record from source {rec.get('source')!r}, expected {cfg['id']!r}")
        out.append(rec)
    if not out:
        raise RuntimeError(f"inbox {path.name} is empty")
    return out


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    inbox = cfg.get("inbox")
    if inbox and not os.environ.get("HS_DIRECT_FETCH") and Path(inbox).exists():
        return _from_inbox(Path(inbox), cfg)
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
