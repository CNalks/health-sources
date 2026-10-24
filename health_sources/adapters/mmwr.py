"""MMWR：feed 很大（2000+ 条、约 2MB），只流式读取最新 N 条；
链接是 tools.cdc.gov 的下载跳转，解析成 cdc.gov/mmwr 原文地址并推出 DOI。"""
from __future__ import annotations

import re

from . import rss


def truncate_feed(payload: bytes, max_items: int) -> bytes:
    """只保留前 max_items 个 <item>，补上闭合标签。"""
    pos = 0
    for _ in range(max_items):
        nxt = payload.find(b"</item>", pos)
        if nxt < 0:
            return payload
        pos = nxt + len(b"</item>")
    return payload[:pos] + b"\n</channel>\n</rss>\n"


def parse(payload: bytes, cfg: dict, fetched_at: str, resolved: dict[str, str] | None = None) -> list[dict]:
    payload = truncate_feed(payload, cfg.get("max_items", 30))
    if resolved:
        def sub(m: re.Match) -> bytes:
            raw = m.group(1).decode("utf-8").replace("&amp;", "&")
            target = resolved.get(raw)
            return b"<link>" + (target or raw).replace("&", "&amp;").encode() + b"</link>"
        payload = re.sub(rb"<link>([^<]+)</link>", sub, payload)
    return rss.parse(payload, cfg, fetched_at)


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    n = cfg.get("max_items", 30)
    payload = truncate_feed(http.get_text_head(cfg["url"], n), n)
    resolved: dict[str, str] = {}
    if cfg.get("resolve_links", True):
        for m in re.finditer(rb"<item>.*?<link>([^<]+)</link>", payload, re.S):
            raw = m.group(1).decode("utf-8").replace("&amp;", "&")
            target = http.resolve_redirect(raw)
            if target:
                resolved[raw] = target
    return parse(payload, cfg, fetched_at, resolved)
