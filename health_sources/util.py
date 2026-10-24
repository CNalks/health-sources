"""通用工具：id、URL 规范化、DOI、日期、HTML 清洗。"""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = {"track", "s_cid", "fbclid", "gclid", "mc_cid", "mc_eid", "ref"}
_DOI_IN_TEXT = re.compile(r"(10\.\d{4,9}/[^\s?#\"'<>]+)", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query), safe="/:"), "")
    )


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.I)
    m = _DOI_IN_TEXT.search(doi)
    return m.group(1).rstrip(".").lower() if m else None


def doi_from_url(url: str) -> str | None:
    """从 URL 中识别 DOI（Eurosurveillance / PLOS 等）以及 EID 的文章路径。"""
    path = urlsplit(url).path + "?" + urlsplit(url).query
    m = _DOI_IN_TEXT.search(path)
    if m:
        return normalize_doi(m.group(1))
    # EID: /eid/article/32/12/25-1991_article -> 10.3201/eid3212.251991
    m = re.search(r"/eid/article/(\d+)/(\d+)/(\d{2})-(\d{4})", url)
    if m:
        vol, issue, yy, num = m.groups()
        return f"10.3201/eid{vol}{int(issue):02d}.{yy}{num}"
    # MMWR: /mmwr/volumes/75/wr/mm7541a2.htm -> 10.15585/mmwr.mm7541a2
    m = re.search(r"/mmwr/volumes/\d+/\w+/((mm|rr|ss)\d{4}a\d+)\.htm", url)
    if m:
        return f"10.15585/mmwr.{m.group(1)}"
    return None


def make_id(doi: str | None, url: str) -> str:
    key = f"doi:{doi.lower()}" if doi else f"url:{normalize_url(url)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def struct_time_to_iso(st: time.struct_time | None) -> str | None:
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc).isoformat()


def parse_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", " ", text)
    text = _TAG_RE.sub("", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def clean_summary(text: str | None, limit: int = 1200) -> str | None:
    s = strip_html(text)
    if not s:
        return None
    if len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s
