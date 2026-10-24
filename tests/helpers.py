from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
NOW = "2026-10-25T02:30:00+00:00"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def source_cfg(source_id: str) -> dict:
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    for s in cfg["sources"]:
        if s["id"] == source_id:
            return dict(s)
    raise KeyError(source_id)


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.closed = False

    def iter_content(self, size=65536):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self):
        self.closed = True


class FakeHttp:
    """离线 HTTP：按 URL 前缀返回 fixture；未登记的 URL 抛错，模拟抓取失败。"""

    def __init__(self, routes: dict[str, bytes], redirects: dict[str, str] | None = None):
        self.routes = routes
        self.redirects = redirects or {}
        self.calls: list[str] = []
        self.params: list = []

    def _lookup(self, url: str) -> bytes:
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return body
        raise ConnectionError(f"offline: {url}")

    def get(self, url, params=None, stream=False):
        self.params.append(params)
        return FakeResponse(self._lookup(url))

    def get_text_head(self, url, max_items, end_tag=b"</item>", max_bytes=8_000_000):
        return self._lookup(url)

    def resolve_redirect(self, url):
        return self.redirects.get(url)
