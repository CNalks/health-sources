"""China CDC Weekly：RSS 1.0/RDF，条目自带 PRISM 卷期字段。

当期：rss/current.xml；往期：rss/{年}_{期}.xml（backfill 用）。
字段解析刻意和公卫前沿 scripts/fetch-ccdc.mjs 的 parseRss 保持一致（同样的标签优先级、
同样的标题清洗），这样下游改读本库后，生成的 data/inbox 与直接读 RSS 完全相同。
feed 里没有作者（<dc:creator> 为空）和栏目（<dc:type> 恒为 article），这两项留空不造。
"""
from __future__ import annotations

import re

from .base import make_record

ALLOWED_HOST = "weekly.chinacdc.cn"


def decode_xml(value: str = "") -> str:
    """等价于 fetch-ccdc.mjs 的 decodeXml（替换顺序也一致）。"""
    value = re.sub(r"^<!\[CDATA\[", "", value)
    value = re.sub(r"\]\]>$", "", value)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        value = value.replace(a, b)
    return value.strip()


def normalize_rss_title(value: str = "") -> str:
    """等价于 fetch-ccdc.mjs 的 normalizeRssTitle。"""
    value = re.sub(r"</?[a-z][a-z0-9]*<", "", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def read_tag(block: str, tag: str) -> str:
    m = re.search(rf"<{re.escape(tag)}(?:\s[^>]*)?>([\s\S]*?)</{re.escape(tag)}>", block, re.I)
    return decode_xml(m.group(1)) if m else ""


def parse_items(xml: str) -> list[dict]:
    """返回按 feed 顺序的原始条目字段（和 parseRss 的 articles 一一对应）。"""
    out = []
    for block in re.findall(r"<item\b[\s\S]*?</item>", xml, re.I):
        url = read_tag(block, "prism:url") or read_tag(block, "link")
        if not url.startswith(f"https://{ALLOWED_HOST}/"):
            raise ValueError(f"unexpected source host: {url[:80]}")
        out.append({
            "doi": read_tag(block, "prism:doi").lower(),
            "title": normalize_rss_title(read_tag(block, "title")),
            "date": read_tag(block, "prism:publicationDate") or read_tag(block, "dc:date"),
            "volume": read_tag(block, "prism:volume"),
            "issue": read_tag(block, "prism:number"),
            "pages": read_tag(block, "prism:startingPage"),
            "url": url,
            "journal": read_tag(block, "prism:publicationName") or "China CDC Weekly",
        })
    return out


def parse(payload: bytes, cfg: dict, fetched_at: str, feed_url: str | None = None) -> list[dict]:
    xml = payload.decode("utf-8", errors="replace")
    items = parse_items(xml)
    records = []
    for order, it in enumerate(items[: cfg.get("max_items", 50)]):
        if not it["title"] or not it["doi"]:
            continue
        date = it["date"][:10] if re.match(r"^\d{4}-\d{2}-\d{2}", it["date"]) else None
        rec = make_record(
            cfg,
            title=it["title"],
            url=it["url"],
            published_at=f"{date}T00:00:00+00:00" if date else None,
            fetched_at=fetched_at,
            doi=it["doi"],
            summary=None,   # feed 描述只有卷期页码，不是摘要；摘要靠 enrich
            meta={"journal": it["journal"], "volume": it["volume"], "issue": it["issue"], "pages": it["pages"],
                  "issue_order": order, "feed_url": feed_url or cfg.get("url")},
        )
        # 标题、链接保留与 fetch-ccdc.mjs 完全相同的写法（不再做通用的 HTML 反转义 / URL 规范化）
        rec["title"] = it["title"]
        rec["url"] = it["url"]
        records.append(rec)
    return records


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    return parse(http.get(cfg["url"]).content, cfg, fetched_at, cfg["url"])
