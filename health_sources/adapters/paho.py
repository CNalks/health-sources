"""PAHO 疫情警报与更新（Epidemiological Alerts and Updates）列表页。

paho.org 按出口 IP 拦截机房地址（阿里云、多数国内/海外云主机返回 403），GitHub Actions 的 runner 可以访问。
所以同一个适配器两种取法：

- 直连（GitHub Actions，`python -m health_sources collect --only paho`）：抓列表页 HTML，解析成统一字段；
- 收件箱（阿里云日常采集）：配置了 `inbox` 且文件存在时，读 Actions 推来的 JSONL（已是统一字段），
  文件超过 `inbox_max_age_hours` 视为过期 → 抛错，runner 照常把该来源标 stale，不造数据。

取标题、日期、原文链接；直连时可选抓页面 meta description 作 summary（不抓正文）。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

from ..util import parse_iso
from .base import make_record

ORIGIN = "https://www.paho.org"
# 列表项：<time datetime="2026-10-20T13:35:52-04:00">…</time> … views-field-title … <a href="/en/documents/…">标题</a>
ITEM_RE = re.compile(
    r'<time datetime="(?P<dt>[^"]+)"[^>]*>.*?views-field-title.*?<a href="(?P<href>/(?:en|es)/documents/[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
KIND_RE = re.compile(r"\b(Epidemiological (?:Alert|Update)|Alerta Epidemiol[oó]gica|Actualizaci[oó]n Epidemiol[oó]gica|"
                     r"Public Health Emergency|Briefing note|Nota informativa)\b", re.I)

META_DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
    re.I,
)


def _page_summary(http, url: str, max_chars: int = 400) -> str | None:
    """直连详情页时取 meta description；失败返回 None，不造文。"""
    try:
        html_text = http.get(url).content.decode("utf-8", "replace")
    except Exception:
        return None
    m = META_DESC_RE.search(html_text)
    if not m:
        return None
    text = html.unescape((m.group(1) or m.group(2) or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) < 40:
        return None
    return text[:max_chars]


def _fill_summaries(records: list[dict], http, cfg: dict) -> None:
    if not cfg.get("fetch_summaries", True):
        return
    delay = float(cfg.get("summary_delay_seconds", 0.4))
    for rec in records:
        if rec.get("summary"):
            continue
        s = _page_summary(http, rec["url"])
        if s:
            rec["summary"] = s
        if delay:
            time.sleep(delay)



def parse(payload: bytes, cfg: dict, fetched_at: str) -> list[dict]:
    text = payload.decode("utf-8", "replace")
    records, seen = [], set()
    for m in ITEM_RE.finditer(text):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m["title"]))).strip()
        url = ORIGIN + m["href"]
        if not title or url in seen:
            continue
        seen.add(url)
        kind_m = KIND_RE.search(title)
        records.append(make_record(
            cfg, title=title, url=url, published_at=parse_iso(m["dt"]), fetched_at=fetched_at,
            native_keywords=[kind_m.group(1).title()] if kind_m else None,
            lang="es" if m["href"].startswith("/es/") else cfg.get("lang", "en"),
            meta={"article_type": kind_m.group(1).title() if kind_m else None, "feed_url": cfg["url"]},
        ))
        if len(records) >= cfg.get("max_items", 30):
            break
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
    pages = int(cfg.get("pages", 1))
    out: list[dict] = []
    for page in range(pages):
        params = {"page": str(page)} if page else None
        out += parse(http.get(cfg["url"], params=params).content, cfg, fetched_at)
    out = out[: cfg.get("max_items", 30)]
    _fill_summaries(out, http, cfg)
    return out
