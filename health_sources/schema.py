"""统一字段定义与校验（不依赖第三方库）。"""
from __future__ import annotations

import re
from datetime import datetime

KINDS = ("article", "news")
STATUSES = ("ok", "stale")

# 字段名 -> (类型, 是否允许为空)
FIELDS: dict[str, tuple[type | tuple[type, ...], bool]] = {
    "id": (str, False),
    "source": (str, False),
    "kind": (str, False),
    "title": (str, False),
    "lang": (str, False),
    "published_at": (str, True),
    "url": (str, False),
    "doi": (str, True),
    "summary": (str, True),
    "keywords": (list, False),
    "license": (str, True),
    "figures_allowed": (bool, False),
    "fetched_at": (str, False),
    "status": (str, False),
    "pmcid": (str, True),
    "enriched_at": (str, True),
    "enrich_source": (str, True),
    # 期刊卷期信息（v0.3）：feed / 往期入口能给就填，给不了就为 null，不硬造
    "journal": (str, True),
    "volume": (str, True),
    "issue": (str, True),
    "pages": (str, True),
    "article_type": (str, True),
    "authors": (list, True),
    "issue_order": (int, True),     # 在本期 feed 里的顺序（0 起），按期组织时用
    "feed_url": (str, True),        # 这条记录最近一次来自哪个 feed / 接口
}

_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_PMCID_RE = re.compile(r"^PMC\d+$")


def _is_iso_with_tz(value: str) -> bool:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.tzinfo is not None


def validate(record: dict) -> list[str]:
    """返回错误列表；空列表表示合法。"""
    errors: list[str] = []
    extra = set(record) - set(FIELDS)
    if extra:
        errors.append(f"unknown fields: {sorted(extra)}")
    for name, (typ, nullable) in FIELDS.items():
        if name not in record:
            errors.append(f"missing field: {name}")
            continue
        value = record[name]
        if value is None:
            if not nullable:
                errors.append(f"{name} must not be null")
            continue
        if not isinstance(value, typ):
            errors.append(f"{name} has wrong type {type(value).__name__}")
            continue
        if typ is str and not nullable and not value.strip():
            errors.append(f"{name} must not be empty")
    if errors:
        return errors
    if not _ID_RE.match(record["id"]):
        errors.append("id must be 16 lowercase hex chars")
    if record["kind"] not in KINDS:
        errors.append(f"kind must be one of {KINDS}")
    if record["status"] not in STATUSES:
        errors.append(f"status must be one of {STATUSES}")
    if not re.match(r"^[a-z]{2}$", record["lang"]):
        errors.append("lang must be ISO 639-1 (2 lowercase letters)")
    if not record["url"].startswith(("http://", "https://")):
        errors.append("url must be http(s)")
    if record["doi"] is not None and not _DOI_RE.match(record["doi"]):
        errors.append("doi malformed")
    if record["pmcid"] is not None and not _PMCID_RE.match(record["pmcid"]):
        errors.append("pmcid must look like PMC123")
    if (record["enriched_at"] is None) != (record["enrich_source"] is None):
        errors.append("enriched_at and enrich_source must be set together")
    for name in ("published_at", "fetched_at", "enriched_at"):
        if record[name] is not None and not _is_iso_with_tz(record[name]):
            errors.append(f"{name} must be ISO 8601 with timezone")
    if record["authors"] is not None and not all(isinstance(a, str) and a.strip() for a in record["authors"]):
        errors.append("authors must be non-empty strings")
    if isinstance(record["issue_order"], bool) or (record["issue_order"] is not None and record["issue_order"] < 0):
        errors.append("issue_order must be a non-negative int")
    kws = record["keywords"]
    if not all(isinstance(k, str) and k.strip() for k in kws):
        errors.append("keywords must be non-empty strings")
    if len(kws) > 10:
        errors.append("keywords at most 10")
    return errors
