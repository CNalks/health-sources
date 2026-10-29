"""向下游订阅方单向、增量推送（阿里云 → 订阅方）。

通用：订阅方配置放 /etc/health-sources/subscribers/<name>.toml（不进仓库），密钥文件单独存放（root 0600，
systemd 用 LoadCredential 交给服务进程）。公卫前沿的订阅方叫 cdc；GHS 以后加一个文件即可。

协议（订阅方实现，见 CDC-Weekly-Digest cloudfunctions/hs-ingest）：
  {action:"cursor"}                         → {ok, data:{records:{updatedAt,id}|null, deleted:{deletedAt,id}|null}}
  {action:"upsert", records, deleted, cursor} → {ok, data:{written, skipped, deleted}}（cursor 省略 = 补推，不动游标）
  {action:"heartbeat", exportGeneratedAt, runStatus, stats} → {ok}
  {action:"reconcile", windowSince, manifest:[[id, content_hash]...]} → {ok, data:{missing:[id], changed:[id], markedDeleted}}

游标以订阅方回执为准：每次运行先问订阅方「最后确认到哪」，所以本机状态丢了也能续传；每批写完才推进，
失败就停，下一次从游标继续。fetched_at、status 不推（每天都变，选篇也不用），避免持续写入。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

SKIP_FIELDS = ("fetched_at", "status")
DEFAULT_CONFIG_DIR = "/etc/health-sources/subscribers"


# ---------------- 记录整形 ----------------

def content_hash(rec: dict, drop: tuple[str, ...] = ()) -> str:
    """对推送内容字段（不含 hs_updated_at / sort_key / content_hash 本身）做规范化 sha256。"""
    skip = set(SKIP_FIELDS) | set(drop) | {"hs_updated_at", "sort_key", "content_hash"}
    body = {k: v for k, v in rec.items() if k not in skip}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def to_push(rec: dict, drop: list[str] | tuple[str, ...] | None = None) -> dict:
    drop_t = tuple(drop or ())
    skip = set(SKIP_FIELDS) | set(drop_t)
    out = {k: v for k, v in rec.items() if k not in skip}
    out["content_hash"] = content_hash(out, drop_t)
    return out


# ---------------- 配置 ----------------

@dataclass
class Subscriber:
    name: str
    transport: str                      # tencent-scf-invoke | https-hmac
    window_days: int = 400
    batch_size: int = 100
    max_requests_per_run: int = 20
    max_body_bytes: int = 900_000       # 单次请求体上限（订阅方限 1 MB）
    # 仅推这些 source（与 CloudBase hs-ingest SOURCES 对齐）；空 = 不限。新闻源等不得进 cdc。
    allowed_sources: list[str] = field(default_factory=list)
    # 额外丢掉的字段（CloudBase FIELDS 不含 tags）；fetched_at/status 仍由 SKIP_FIELDS 处理
    drop_fields: list[str] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)


def _read_kv(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_subscriber(name: str, config_dir: str | Path = DEFAULT_CONFIG_DIR) -> Subscriber:
    import tomllib
    cfg = tomllib.loads((Path(config_dir) / f"{name}.toml").read_text(encoding="utf-8"))
    sub = Subscriber(
        name=name, transport=cfg["transport"],
        window_days=int(cfg.get("window_days", 400)), batch_size=int(cfg.get("batch_size", 100)),
        max_requests_per_run=int(cfg.get("max_requests_per_run", 20)),
        max_body_bytes=int(cfg.get("max_body_bytes", 900_000)),
        allowed_sources=list(cfg.get("allowed_sources") or []),
        drop_fields=list(cfg.get("drop_fields") or []),
        options=dict(cfg.get(cfg["transport"], {})),
    )
    # 密钥：优先 systemd LoadCredential（$CREDENTIALS_DIRECTORY/<name>.secret），否则 toml 里的 credentials_file
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    cred = Path(cred_dir) / f"{name}.secret" if cred_dir else None
    if not (cred and cred.exists()):
        cred = Path(cfg["credentials_file"]) if cfg.get("credentials_file") else None
    if cred:
        sub.credentials = _read_kv(cred)
    return sub


# ---------------- 通道 ----------------

class TransportError(RuntimeError):
    pass


class ScfInvokeTransport:
    """A3：腾讯云 CAM 子用户密钥（只允许对一个函数 scf:InvokeFunction）+ TC3 签名直接调用云函数。无公网路由。"""
    host = "scf.tencentcloudapi.com"
    service = "scf"
    version = "2018-04-16"

    def __init__(self, secret_id: str, secret_key: str, namespace: str, function: str,
                 region: str = "ap-shanghai", token: str | None = None, session=None, clock=time.time):
        if not secret_id or not secret_key:
            raise TransportError("missing SCF credentials")
        self._id, self._key = secret_id, secret_key
        self.namespace, self.function, self.region, self.token = namespace, function, region, token
        self.clock = clock
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def _headers(self, action: str, payload: str) -> dict:
        ts = int(self.clock())
        date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
        sha = lambda s: hashlib.sha256(s.encode("utf-8")).hexdigest()  # noqa: E731
        canon = "\n".join(["POST", "/", "", f"content-type:application/json\nhost:{self.host}\n",
                           "content-type;host", sha(payload)])
        scope = f"{date}/{self.service}/tc3_request"
        sts = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, sha(canon)])
        k = lambda key, msg: hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()  # noqa: E731
        signing = k(k(k(("TC3" + self._key).encode("utf-8"), date), self.service), "tc3_request")
        sig = hmac.new(signing, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "Authorization": f"TC3-HMAC-SHA256 Credential={self._id}/{scope}, SignedHeaders=content-type;host, Signature={sig}",
            "Content-Type": "application/json", "Host": self.host, "X-TC-Action": action,
            "X-TC-Timestamp": str(ts), "X-TC-Version": self.version, "X-TC-Region": self.region,
        }

    def call(self, body: dict) -> dict:
        if self.token:
            body = {**body, "token": self.token}
        payload = json.dumps({
            "FunctionName": self.function, "Namespace": self.namespace, "Qualifier": "$LATEST",
            "InvocationType": "RequestResponse", "ClientContext": json.dumps(body, ensure_ascii=False),
        }, ensure_ascii=False)
        resp = self.session.post(f"https://{self.host}/", data=payload.encode("utf-8"),
                                 headers=self._headers("Invoke", payload), timeout=60)
        r = resp.json().get("Response", {})
        if r.get("Error"):
            raise TransportError(f"Invoke: {r['Error'].get('Code')} {r['Error'].get('Message')}")
        result = r.get("Result") or {}
        if result.get("InvokeResult", 0) != 0 or result.get("ErrMsg"):
            raise TransportError(f"function error: {str(result.get('ErrMsg'))[:300]}")
        return _check(json.loads(result.get("RetMsg") or "null"))


class HmacHttpsTransport:
    """A1：HTTP 路由 + HMAC-SHA256。签名串 = "<unix 秒>.<nonce 32 位十六进制>.<原始请求体>"，
    头 X-HS-Timestamp / X-HS-Nonce / X-HS-Signature。订阅方要求时间差 ≤300 秒、nonce 不可重用。"""

    def __init__(self, url: str, secret: str, session=None, clock=time.time, nonce=None):
        if not url.startswith("https://") or not secret:
            raise TransportError("https-hmac needs an https url and a secret")
        self.url, self._secret, self.clock = url, secret.encode("utf-8"), clock
        self.nonce = nonce or (lambda: secrets.token_hex(16))
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def call(self, body: dict) -> dict:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        ts = str(int(self.clock()))
        nonce = self.nonce()
        sig = hmac.new(self._secret, ts.encode() + b"." + nonce.encode() + b"." + raw, hashlib.sha256).hexdigest()
        resp = self.session.post(self.url, data=raw, timeout=60, headers={
            "Content-Type": "application/json", "X-HS-Timestamp": ts, "X-HS-Nonce": nonce, "X-HS-Signature": sig})
        status = getattr(resp, "status_code", 200)
        try:
            data = resp.json()
        except ValueError:
            raise TransportError(f"subscriber returned non-JSON (HTTP {status})") from None
        if isinstance(data, dict) and "ok" not in data and data.get("code"):
            # 网关自身的错误（如 EXCEED_MAX_PAYLOAD_SIZE），不是 hs-ingest 的返回
            raise TransportError(f"gateway HTTP {status}: {str(data.get('code'))[:80]}")
        return _check(data)


def _check(res) -> dict:
    if not isinstance(res, dict) or not res.get("ok"):
        err = (res or {}).get("error") if isinstance(res, dict) else res
        raise TransportError(f"subscriber rejected: {json.dumps(err, ensure_ascii=False)[:300]}")
    return res.get("data") or {}


def make_transport(sub: Subscriber):
    o, c = sub.options, sub.credentials
    if sub.transport == "tencent-scf-invoke":
        return ScfInvokeTransport(c.get("TENCENTCLOUD_SECRETID", ""), c.get("TENCENTCLOUD_SECRETKEY", ""),
                                  namespace=o["namespace"], function=o.get("function", "hs-ingest"),
                                  region=o.get("region", "ap-shanghai"), token=c.get("HS_INGEST_TOKEN") or None)
    if sub.transport == "https-hmac":
        return HmacHttpsTransport(o["url"], c.get("HS_INGEST_SECRET", ""))
    raise TransportError(f"unknown transport {sub.transport}")


# ---------------- 推送 / 对账 ----------------

def window_since(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).replace(microsecond=0).isoformat()


def _fit(items: list, max_bytes: int) -> list:
    """按请求体上限截断（至少保留 1 条）。"""
    out, size = [], 0
    for it in items:
        n = len(json.dumps(it, ensure_ascii=False).encode("utf-8"))
        if out and size + n > max_bytes:
            break
        out.append(it)
        size += n
    return out


def push(store: Store, transport, sub: Subscriber, export_generated_at: str | None = None,
         now: datetime | None = None, run_status: str = "ok") -> dict:
    now = now or datetime.now(timezone.utc)
    since = window_since(now, sub.window_days)
    sources = sub.allowed_sources or None
    drop = sub.drop_fields or []
    cur = transport.call({"action": "cursor"})
    rc, dc = cur.get("records"), cur.get("deleted")
    stats = {"requests": 1, "records": 0, "deleted": 0, "written": 0, "skipped": 0, "complete": False,
             "cursorBefore": {"records": rc, "deleted": dc}}
    try:
        for _ in range(sub.max_requests_per_run):
            raw = store.changed_since(rc, since, sub.batch_size, sources=sources)
            recs = _fit([to_push(r, drop) for r in raw], sub.max_body_bytes)
            dels = store.tombstones_since(dc, sub.batch_size, sources=sources)
            if not recs and not dels:
                stats["complete"] = True
                break
            nrc = {"updatedAt": recs[-1]["hs_updated_at"], "id": recs[-1]["id"]} if recs else rc
            ndc = {"deletedAt": dels[-1]["deleted_at"], "id": dels[-1]["id"]} if dels else dc
            res = transport.call({
                "action": "upsert", "subscriber": sub.name, "records": recs,
                "deleted": [{"id": d["id"], "source": d["source"], "deletedAt": d["deleted_at"], "reason": d["reason"]} for d in dels],
                "cursor": {"records": nrc, "deleted": ndc},
            })
            stats["requests"] += 1
            stats["records"] += len(recs)
            stats["deleted"] += len(dels)
            stats["written"] += int(res.get("written", 0))
            stats["skipped"] += int(res.get("skipped", 0))
            rc, dc = nrc, ndc              # 订阅方确认后才推进
    finally:
        stats["cursorAfter"] = {"records": rc, "deleted": dc}
    # 心跳：每次运行结束都发（订阅方据此判断导出是否已更新），不含条目
    transport.call({"action": "heartbeat", "subscriber": sub.name, "exportGeneratedAt": export_generated_at,
                    "runStatus": run_status if stats["complete"] else "partial",
                    "stats": {k: stats[k] for k in ("records", "deleted", "written", "skipped", "complete")}})
    stats["requests"] += 1
    return stats


def reconcile(store: Store, transport, sub: Subscriber, now: datetime | None = None) -> dict:
    """每周对账：发窗口内全部 id + content_hash，订阅方回「缺失 / hash 不同」，这里补推（不动游标）；
    清单里没有、订阅方还没标删除的，由订阅方标 deleted（reason=reconcile）。"""
    now = now or datetime.now(timezone.utc)
    since = window_since(now, sub.window_days)
    sources = sub.allowed_sources or None
    drop = sub.drop_fields or []
    by_id = {}
    manifest = []
    for r in store.window_records(since, sources=sources):
        p = to_push(r, drop)
        manifest.append([p["id"], p["content_hash"]])
        by_id[p["id"]] = p
    res = transport.call({"action": "reconcile", "subscriber": sub.name, "windowSince": since, "manifest": manifest})
    todo = [by_id[i] for i in [*res.get("missing", []), *res.get("changed", [])] if i in by_id]
    sent = 0
    requests = 1
    while todo and requests <= sub.max_requests_per_run:
        chunk = _fit(todo[:sub.batch_size], sub.max_body_bytes)
        transport.call({"action": "upsert", "subscriber": sub.name, "records": chunk, "deleted": []})
        todo = todo[len(chunk):]
        sent += len(chunk)
        requests += 1
    return {"manifest": len(manifest), "missing": len(res.get("missing", [])), "changed": len(res.get("changed", [])),
            "resent": sent, "leftover": len(todo), "markedDeleted": res.get("markedDeleted", 0), "requests": requests}


class DryRunTransport:
    """不联网：游标为空（等于首次全量），统计会发多少。"""

    def __init__(self):
        self.calls: list[dict] = []

    def call(self, body: dict) -> dict:
        self.calls.append({"action": body["action"], "records": len(body.get("records") or []),
                           "deleted": len(body.get("deleted") or []),
                           "bytes": len(json.dumps(body, ensure_ascii=False).encode("utf-8"))})
        if body["action"] == "cursor":
            return {"records": None, "deleted": None}
        if body["action"] == "reconcile":
            return {"missing": [m[0] for m in body["manifest"]], "changed": [], "markedDeleted": 0}
        return {"written": len(body.get("records") or []), "skipped": 0}


def read_export_generated_at(data_dir: Path) -> str | None:
    try:
        return json.loads((Path(data_dir) / "status.json").read_text(encoding="utf-8")).get("generated_at")
    except (OSError, ValueError):
        return None


__all__ = ["push", "reconcile", "load_subscriber", "make_transport", "DryRunTransport", "ScfInvokeTransport",
           "HmacHttpsTransport", "content_hash", "to_push", "TransportError"]
