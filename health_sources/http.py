"""极简 HTTP 客户端：超时、重试、统一 UA。"""
from __future__ import annotations

import time

import requests


class Http:
    def __init__(self, user_agent: str, timeout: float = 40, retries: int = 2, backoff: float = 3.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def get(self, url: str, params: dict | None = None, stream: bool = False) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, stream=stream)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # noqa: PERF203
                last = exc
                if attempt < self.retries:
                    time.sleep(self.backoff * (attempt + 1))
        assert last is not None
        raise last

    def get_text_head(self, url: str, max_items: int, end_tag: bytes = b"</item>", max_bytes: int = 8_000_000) -> bytes:
        """流式读取大 feed，读够 max_items 个条目就停止，避免整份 2MB+ 文件进内存解析。"""
        resp = self.get(url, stream=True)
        buf = bytearray()
        try:
            for chunk in resp.iter_content(64 * 1024):
                buf.extend(chunk)
                if buf.count(end_tag) >= max_items or len(buf) > max_bytes:
                    break
        finally:
            resp.close()
        return bytes(buf)

    def resolve_redirect(self, url: str) -> str | None:
        try:
            resp = self.session.head(url, allow_redirects=False, timeout=self.timeout)
        except requests.RequestException:
            return None
        loc = resp.headers.get("Location")
        return loc if resp.is_redirect and loc else None
