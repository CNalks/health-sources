"""可选规范化标签：disease_tags 引用公卫前沿病种词表 code；country_iso3 为 ISO 3166-1 alpha-3。

词表权威在 CDC-Weekly-Digest/data/vocab/diseases.json，本库不维护第二份词表。
加载顺序：环境变量 HS_VOCAB_DISEASES → 同级/部署路径候选 → 找不到则 disease_tags 一律 []。
国家标签不依赖 regions 词表，用内置别名 +（可选）pycountry。
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

_VOCAB_CANDIDATES = (
    os.environ.get("HS_VOCAB_DISEASES"),
    "/opt/cdc-weekly-digest/data/vocab/diseases.json",
    "/var/lib/cdc-weekly/vocab/diseases.json",
    str(Path(__file__).resolve().parents[2] / "CDC-Weekly-Digest" / "data" / "vocab" / "diseases.json"),
    str(Path(__file__).resolve().parents[1] / "data" / "vocab" / "diseases.json"),
)

# WHO/ECDC 常见写法（与 GHS pipeline/events.py 对齐的子集）
_COUNTRY_ALIASES = {
    "russian federation": "RUS", "viet nam": "VNM", "united republic of tanzania": "TZA",
    "türkiye": "TUR", "turkey": "TUR", "côte d’ivoire": "CIV", "côte d'ivoire": "CIV",
    "cote d'ivoire": "CIV", "democratic republic of the congo": "COD", "drc": "COD",
    "congo": "COG", "republic of the congo": "COG", "united states of america": "USA",
    "united states": "USA", "united kingdom": "GBR", "hong kong": "HKG", "hong kong sar": "HKG",
    "macao": "MAC", "china": "CHN", "saudi arabia": "SAU", "kingdom of saudi arabia": "SAU",
    "south korea": "KOR", "republic of korea": "KOR", "north korea": "PRK",
    "iran (islamic republic of)": "IRN", "bolivia (plurinational state of)": "BOL",
    "venezuela (bolivarian republic of)": "VEN", "laos": "LAO", "myanmar": "MMR",
    "eswatini": "SWZ", "czechia": "CZE", "cabo verde": "CPV", "timor-leste": "TLS",
}


def _word_re(term: str, ignore_case: bool = True) -> re.Pattern:
    flags = re.I if ignore_case else 0
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", flags)


@lru_cache(maxsize=1)
def _disease_patterns() -> list[tuple[str, re.Pattern]]:
    # HS_VOCAB_DISEASES 若显式设置：只认这一条（可用来强制关闭：指向不存在路径）
    env = os.environ.get("HS_VOCAB_DISEASES")
    if env is not None:
        path = env if Path(env).is_file() else None
    else:
        path = next((p for p in _VOCAB_CANDIDATES[1:] if p and Path(p).is_file()), None)
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[tuple[str, re.Pattern]] = []
    for item in data.get("items") or []:
        code = item.get("code")
        if not code:
            continue
        terms = []
        for a in item.get("aliases") or []:
            if a and len(a) >= 3:
                terms.append(a)
        for a in item.get("acronyms") or []:
            if a:
                terms.append(a)
        en = item.get("en")
        if en and len(en) >= 3:
            terms.append(en)
        # longer first to prefer specific matches
        for term in sorted(set(terms), key=len, reverse=True):
            # acronyms: case-sensitive when all-caps and short
            ignore = not (term.isupper() and len(term) <= 6 and term.isalpha())
            out.append((code, _word_re(term, ignore_case=ignore)))
    return out


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    idx = dict(_COUNTRY_ALIASES)
    try:
        import pycountry
        for c in pycountry.countries:
            for n in {c.name, getattr(c, "common_name", None), getattr(c, "official_name", None)}:
                if n:
                    idx.setdefault(n.lower(), c.alpha_3)
    except Exception:
        pass
    return idx


def disease_tags(title: str, keywords: list[str] | None = None, limit: int = 5) -> list[str]:
    blob = " ".join([title or ""] + list(keywords or []))
    if not blob.strip():
        return []
    found: list[str] = []
    seen: set[str] = set()
    for code, pat in _disease_patterns():
        if code in seen:
            continue
        if pat.search(blob):
            seen.add(code)
            found.append(code)
            if len(found) >= limit:
                break
    return found


def country_iso3(title: str, limit: int = 5) -> list[str]:
    t = " " + re.sub(r"\s+", " ", (title or "").lower()) + " "
    idx = _country_index()
    found: list[str] = []
    for name in sorted(idx, key=len, reverse=True):
        if len(name) < 4 and name not in ("usa", "drc"):
            continue
        if re.search(r"(?<![a-zà-ÿ])" + re.escape(name) + r"(?![a-zà-ÿ])", t):
            iso = idx[name]
            if iso not in found:
                found.append(iso)
            t = t.replace(name, " " * len(name))
            if len(found) >= limit:
                break
    return found


def apply_tags(record: dict) -> dict:
    """就地补全可选标签；已有非空值不覆盖。"""
    if not record.get("disease_tags"):
        record["disease_tags"] = disease_tags(record.get("title") or "", record.get("keywords"))
    if not record.get("country_iso3"):
        record["country_iso3"] = country_iso3(record.get("title") or "")
    return record
