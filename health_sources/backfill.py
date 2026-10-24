"""往期回补：按各来源自己的往期入口分批拉取历史条目，写入同一个 SQLite（按 id 去重/合并）。

原则：
- 只走官方的 feed / API 往期入口，不抓 HTML 网页；没有往期入口的来源记下来跳过；
- 每次请求之间 sleep（sources.yaml 里每个来源 backfill.delay_seconds）；
- 分批：每页 / 每期解析完立即写库，不把整份历史读进内存（MMWR 全量 feed 流式切块）；
- 不改 source_runs（那是每日采集的健康状态），回补结果单独写 data_dir/backfill/<来源>.json。

入口（sources.yaml 的 backfill.method）：
  ccdc_issues   China CDC Weekly 往期 RSS：rss/{year}_{issue}.xml
  full_feed     MMWR 全量 RSS（2000+ 条，条目不按时间排序）：流式按块解析，只留 since 之后的
  crossref      Crossref 按 ISSN 列出作品（Eurosurveillance、EID 没有往期 feed，用 DOI 注册机构的官方 API）
  odata_pages   WHO DON 官方 JSON 接口 $skip 分页
  feed_pages    ECDC taxonomy feed ?page=N 分页
  plos_search   PLOS 官方搜索 API（Solr），按发布日期过滤
"""
from __future__ import annotations

import email.utils
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .adapters import ccdc, mmwr, rss, who_don
from .adapters.base import make_record
from .schema import validate
from .util import clean_summary, parse_iso, strip_html

log = logging.getLogger("health_sources.backfill")

Batch = list[dict]


class Ctx:
    def __init__(self, http, now: str, since: str, sleep: Callable[[float], None] = time.sleep):
        self.http = http
        self.now = now
        self.since = since              # ISO 日期 YYYY-MM-DD（含）
        self.sleep = sleep
        self.requests = 0
        self.notes: list[str] = []

    def get(self, url: str, delay: float, **kw):
        if self.requests:
            self.sleep(delay)
        self.requests += 1
        return self.http.get(url, **kw)

    def in_window(self, rec: dict) -> bool:
        return bool(rec.get("published_at")) and rec["published_at"][:10] >= self.since


# ---------------------------------------------------------------- ccdc_issues

def parse_issue_range(spec: str) -> list[int]:
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def bf_ccdc_issues(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    year = int(opts.get("year") or conf.get("year") or datetime.now(timezone.utc).year)
    issues = parse_issue_range(opts.get("issues") or conf.get("issues", ""))
    if not issues:
        raise ValueError("ccdc_issues needs --issues, e.g. 28-43")
    cfg = {**src, "max_items": 200}
    for n in issues:
        url = conf["url_template"].format(year=year, issue=n)
        try:
            payload = ctx.get(url, conf.get("delay_seconds", 6)).content
            recs = ccdc.parse(payload, cfg, ctx.now, url)
        except Exception as exc:  # noqa: BLE001 —— 单期失败记下来，继续下一期
            ctx.notes.append(f"{year}_{n}: {type(exc).__name__}: {exc}")
            continue
        if not recs:
            ctx.notes.append(f"{year}_{n}: feed has no items")
            continue
        # 期号必须对上；日期为空的条目（官网 feed 偶有，如法定传染病月报）保留，不推测日期
        wrong = [r for r in recs if r["issue"] != str(n)
                 or (r["published_at"] and not r["published_at"].startswith(str(year)))]
        if wrong:
            ctx.notes.append(f"{year}_{n}: {len(wrong)} item(s) carry other issue metadata, dropped")
            recs = [r for r in recs if r not in wrong]
        yield recs


# ---------------------------------------------------------------- full_feed (MMWR)

def _item_date(item: bytes) -> str | None:
    m = re.search(rb"<pubDate>([^<]+)</pubDate>", item)
    if not m:
        return None
    try:
        return email.utils.parsedate_to_datetime(m.group(1).decode()).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return None


def iter_feed_chunks(resp, batch_items: int, end_tag: bytes = b"</item>") -> Iterator[tuple[bytes, list[bytes]]]:
    """流式切出 (feed 头, 一批 <item>…</item>)；内存里最多一批条目 + 一个网络块。"""
    buf = b""
    head: bytes | None = None
    items: list[bytes] = []
    for chunk in resp.iter_content(64 * 1024):
        buf += chunk
        if head is None:
            i = buf.find(b"<item>")
            if i < 0:
                continue
            head, buf = buf[:i], buf[i:]
        while True:
            j = buf.find(end_tag)
            if j < 0:
                break
            j += len(end_tag)
            items.append(buf[:j].strip())
            buf = buf[j:]
            if len(items) >= batch_items:
                yield head, items
                items = []
    if head is not None and items:
        yield head, items


def bf_full_feed(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    delay = conf.get("delay_seconds", 0.5)
    resp = ctx.get(src["url"], delay, stream=True)
    total = kept_total = 0
    cfg = {**src, "max_items": 10**6}
    try:
        for head, items in iter_feed_chunks(resp, conf.get("batch_items", 100)):
            total += len(items)
            kept = [it for it in items if (_item_date(it) or "") >= ctx.since]
            kept_total += len(kept)
            if not kept:
                continue
            resolved: dict[str, str] = {}
            if src.get("resolve_links", True):
                for it in kept:
                    m = re.search(rb"<link>([^<]+)</link>", it)
                    if not m:
                        continue
                    raw = m.group(1).decode("utf-8").replace("&amp;", "&")
                    ctx.sleep(delay)
                    ctx.requests += 1
                    target = ctx.http.resolve_redirect(raw)
                    if target:
                        resolved[raw] = target
            payload = head + b"\n".join(kept) + b"\n</channel>\n</rss>\n"
            recs = mmwr.parse(payload, cfg, ctx.now, resolved)
            for r in recs:
                r["feed_url"] = src["url"]
            yield [r for r in recs if ctx.in_window(r)]
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()
    ctx.notes.append(f"scanned {total} feed items, {kept_total} on/after {ctx.since}")


# ---------------------------------------------------------------- crossref

def _date_parts(item: dict, today: str | None = None) -> str | None:
    """优先取精确到日的出版日期；Crossref 只给到「年-月」时（EID 常见）改用 DOI 注册时间 created
    （约等于上线日期），再不行才记为当月 1 日。明显不合理的未来日期（> 今天 + 90 天，如元数据把 2019 录成 2109）跳过。"""
    limit = None
    if today:
        y, m, d = (int(x) for x in today[:10].split("-"))
        limit = f"{y + (m + 3 > 12):04d}-{(m + 3 - 1) % 12 + 1:02d}-{d:02d}"
    month_only = None
    for key in ("published-online", "published-print", "published", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if not parts or not parts[0]:
            continue
        y, m, d = (list(parts) + [None, None])[:3]
        iso = f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
        if limit and iso > limit:
            continue
        if d:
            return f"{iso}T00:00:00+00:00"
        month_only = month_only or iso
    created = parse_iso((item.get("created") or {}).get("date-time"))
    if created and (not limit or created[:10] <= limit):
        return created
    return f"{month_only}T00:00:00+00:00" if month_only else None


def eid_url(doi: str) -> str | None:
    m = re.fullmatch(r"10\.3201/eid(\d{2})(\d{2})\.(\d{2})(\d{4})", doi.lower())
    if not m:
        return None
    vol, issue, yy, num = m.groups()
    return f"https://wwwnc.cdc.gov/eid/article/{int(vol)}/{int(issue)}/{yy}-{num}_article"


def crossref_record(item: dict, src: dict, conf: dict, now: str) -> dict | None:
    doi = (item.get("DOI") or "").lower()
    title = strip_html(" ".join(item.get("title") or []))
    if not doi or not title:
        return None
    style = conf.get("url_style")
    if style == "eid":
        url = eid_url(doi)
    elif style == "prefix":
        url = conf["url_prefix"] + item["DOI"]
    else:
        url = None
    url = url or ((item.get("resource") or {}).get("primary") or {}).get("URL") or item.get("URL") or f"https://doi.org/{doi}"
    authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "")
               for a in item.get("author") or []]
    abstract = item.get("abstract")
    if abstract:
        abstract = re.sub(r"<jats:title>[^<]*</jats:title>", " ", abstract)
    return make_record(
        src, title=title, url=url, published_at=_date_parts(item, now), fetched_at=now, doi=doi,
        summary=clean_summary(abstract) if abstract else None,
        native_keywords=item.get("subject") or None,
        meta={"journal": (item.get("container-title") or [None])[0], "volume": item.get("volume"),
              "issue": item.get("issue"), "pages": item.get("page"), "authors": authors,
              "feed_url": conf["url"].format(issn=conf["issn"])},
    )


def bf_crossref(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    url = conf["url"].format(issn=conf["issn"])
    cursor = "*"
    rows = conf.get("rows", 100)
    seen = 0
    for _ in range(conf.get("max_pages", 30)):
        params = {"filter": f"from-pub-date:{ctx.since}", "rows": str(rows), "cursor": cursor,
                  "select": "DOI,title,published-online,published-print,published,issued,volume,issue,page,"
                            "author,URL,resource,container-title,abstract,subject,type,created"}
        data = json.loads(ctx.get(url, conf.get("delay_seconds", 1.0), params=params).content)["message"]
        items = data.get("items") or []
        if not items:
            break
        seen += len(items)
        recs = [r for r in (crossref_record(i, src, conf, ctx.now) for i in items) if r]
        yield [r for r in recs if ctx.in_window(r)]
        cursor = data.get("next-cursor")
        if not cursor or len(items) < rows:
            break
    ctx.notes.append(f"crossref returned {seen} works since {ctx.since}")


# ---------------------------------------------------------------- odata_pages (WHO DON)

def bf_odata_pages(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    size = conf.get("page_size", 50)
    cfg = {**src, "max_items": size}
    for page in range(conf.get("max_pages", 20)):
        params = {"$orderby": "PublicationDateAndTime desc", "$top": str(size), "$skip": str(page * size),
                  "$select": who_don.FIELDS}
        payload = ctx.get(src["url"], conf.get("delay_seconds", 1.0), params=params).content
        recs = who_don.parse(payload, cfg, ctx.now)
        for r in recs:
            r["feed_url"] = src["url"]
        if not recs:
            break
        yield [r for r in recs if ctx.in_window(r)]
        oldest = min((r["published_at"] or "9999")[:10] for r in recs)
        if oldest < ctx.since or len(recs) < size:
            break


# ---------------------------------------------------------------- feed_pages (ECDC)

def bf_feed_pages(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    param = conf.get("page_param", "page")
    feeds = src.get("feeds") or [{"url": src["url"]}]
    cfg = {**src, "max_items": 10**6}
    for f in feeds:
        prev_ids: set[str] = set()
        for page in range(conf.get("max_pages", 15)):
            url = f["url"] + ("&" if "?" in f["url"] else "?") + f"{param}={page}"
            try:
                payload = ctx.get(url, conf.get("delay_seconds", 2.0)).content
                recs = rss.parse(payload, cfg, ctx.now, f.get("category"))
            except Exception as exc:  # noqa: BLE001
                ctx.notes.append(f"{url}: {type(exc).__name__}: {exc}")
                break
            ids = {r["id"] for r in recs}
            if not recs or ids <= prev_ids:   # 空页，或服务器忽略分页参数返回同一页
                if recs and page:
                    ctx.notes.append(f"{f['url']}: page={page} returned the same items as before (pagination ignored)")
                break
            prev_ids |= ids
            for r in recs:
                r["feed_url"] = f["url"]
            yield [r for r in recs if ctx.in_window(r)]
            oldest = min((r["published_at"] or "9999")[:10] for r in recs)
            if oldest < ctx.since:
                break


# ---------------------------------------------------------------- plos_search

def plos_record(doc: dict, src: dict, conf: dict, now: str) -> dict | None:
    doi = (doc.get("id") or "").lower()
    title = strip_html(doc.get("title_display") or "")
    if not doi or not title:
        return None
    abstract = doc.get("abstract")
    if isinstance(abstract, list):
        abstract = " ".join(abstract)
    return make_record(
        src, title=title, url=conf["article_url"].format(doi=doc["id"]),
        published_at=parse_iso(doc.get("publication_date")), fetched_at=now, doi=doi,
        summary=clean_summary(abstract) if abstract else None,
        meta={"journal": doc.get("journal"), "volume": doc.get("volume"), "issue": doc.get("issue"),
              "article_type": doc.get("article_type"), "authors": doc.get("author_display"),
              "feed_url": conf["url"]},
    )


def bf_plos_search(src: dict, ctx: Ctx, opts: dict) -> Iterator[Batch]:
    conf = src["backfill"]
    rows = conf.get("rows", 100)
    for page in range(conf.get("max_pages", 20)):
        params = {"q": f"journal_key:{conf['journal_key']}",
                  "fq": ["doc_type:full", f"publication_date:[{ctx.since}T00:00:00Z TO NOW]"],
                  "fl": "id,title_display,publication_date,abstract,article_type,author_display,volume,issue,journal",
                  "sort": "publication_date desc", "rows": str(rows), "start": str(page * rows), "wt": "json"}
        data = json.loads(ctx.get(conf["url"], conf.get("delay_seconds", 1.0), params=params).content)["response"]
        docs = data.get("docs") or []
        if not docs:
            break
        recs = [r for r in (plos_record(d, src, conf, ctx.now) for d in docs) if r]
        yield [r for r in recs if ctx.in_window(r)]
        if (page + 1) * rows >= int(data.get("numFound", 0)):
            break


METHODS = {
    "ccdc_issues": bf_ccdc_issues,
    "full_feed": bf_full_feed,
    "crossref": bf_crossref,
    "odata_pages": bf_odata_pages,
    "feed_pages": bf_feed_pages,
    "plos_search": bf_plos_search,
}


def backfill_source(src: dict, store, http, now: str, since: str, opts: dict | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    opts = opts or {}
    conf = src.get("backfill") or {}
    method = conf.get("method")
    out = {"source": src["id"], "method": method, "since": since, "ok": False, "batches": 0, "records": 0,
           "new": 0, "updated": 0, "unchanged": 0, "invalid": 0, "requests": 0, "earliest": None, "latest": None,
           "notes": []}
    if not method:
        out["notes"].append(conf.get("skip_reason") or "no backfill entry configured, skipped")
        out["skipped"] = True
        return out
    ctx = Ctx(http, now, since, sleep)
    started = time.monotonic()
    try:
        for batch in METHODS[method](src, ctx, opts):
            good = []
            for r in batch:
                errs = validate(r)
                if errs:
                    out["invalid"] += 1
                    if out["invalid"] <= 5:
                        ctx.notes.append(f"invalid {r.get('url')}: {errs}")
                else:
                    good.append(r)
            uniq = list({r["id"]: r for r in good}.values())
            if uniq:
                st = store.upsert(uniq, now)
                for k in ("new", "updated", "unchanged"):
                    out[k] += st[k]
                out["records"] += len(uniq)
                dates = sorted(r["published_at"][:10] for r in uniq if r["published_at"])
                if dates:
                    out["earliest"] = min(filter(None, [out["earliest"], dates[0]]))
                    out["latest"] = max(filter(None, [out["latest"], dates[-1]]))
            out["batches"] += 1
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 —— 一个来源失败不影响其他来源
        out["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("backfill %s FAILED: %s", src["id"], out["error"])
    out["requests"] = ctx.requests
    out["notes"] += ctx.notes
    out["seconds"] = round(time.monotonic() - started, 1)
    log.info("backfill %s: %s", src["id"], {k: out[k] for k in ("ok", "records", "new", "updated", "invalid",
                                                                 "requests", "earliest", "latest")})
    return out


def write_report(data_dir: Path, result: dict) -> None:
    d = Path(data_dir) / "backfill"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{result['source']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
