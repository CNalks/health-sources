"""WHO Disease Outbreak News：官方 JSON 接口，按 PublicationDateAndTime 倒序取最新 N 条。"""
from __future__ import annotations

import json

from ..util import parse_iso
from .base import make_record

FIELDS = "Id,Title,OverrideTitle,UseOverrideTitle,Summary,PublicationDateAndTime,UrlName,DonId,ItemDefaultUrl"


def parse(payload: bytes, cfg: dict, fetched_at: str) -> list[dict]:
    data = json.loads(payload)
    prefix = cfg.get("item_url_prefix", "https://www.who.int/emergencies/disease-outbreak-news/item/")
    records = []
    for item in data.get("value", [])[: cfg.get("max_items", 20)]:
        title = item.get("OverrideTitle") if item.get("UseOverrideTitle") and item.get("OverrideTitle") else item.get("Title")
        slug = item.get("UrlName") or (item.get("ItemDefaultUrl") or "").strip("/")
        if not title or not slug:
            continue
        records.append(
            make_record(
                cfg,
                title=title,
                url=prefix + slug,
                published_at=parse_iso(item.get("PublicationDateAndTime")),
                fetched_at=fetched_at,
                summary=item.get("Summary"),
            )
        )
    return records


def fetch(cfg: dict, http, fetched_at: str) -> list[dict]:
    params = {
        "$orderby": "PublicationDateAndTime desc",
        "$top": str(cfg.get("max_items", 20)),
        "$select": FIELDS,
    }
    return parse(http.get(cfg["url"], params=params).content, cfg, fetched_at)
