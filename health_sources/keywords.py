"""关键词：优先原文自带（期刊关键词 / RSS category），否则从标题按规则提取 3~5 个。

标题提取是一个精简版 RAKE：用停用词和标点把标题切成候选短语，
按「词度/词频」打分，取得分最高的若干短语。不调用任何模型。
"""
from __future__ import annotations

import re

from .util import strip_html

STOPWORDS = set(
    """
a about above after against all among an and any are as at be been before being below
between both but by can could did do does during each few for from further had has have
having here how i if in into is it its itself just more most no nor not of off on once only
or other our out over own same should so some such than that the their them then there these
they this those through to too under until up very via was we were what when where which while
who whom why will with within without would you your
among versus vs per using use used based case cases report reports study studies analysis
associated association among new update updates notes field note brief toward towards across
following potential evidence among patients patient persons people adults children
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec week weeks year years month months
united states us usa eu eea
caused due among identification characterization characteristics among among
""".split()
)

GENERIC = {"disease", "diseases", "infection", "infections", "virus", "health", "public", "risk"}

_SPLIT_RE = re.compile(r"[,:;()\[\]\"“”‘’!?|/]|\s[-–—]\s|\.\s")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*[A-Za-z0-9]|[A-Za-z]")


def _clean_kw(k: str) -> str:
    return re.sub(r"\s+", " ", strip_html(k)).strip(" .;,")


def from_native(candidates: list[str] | None, ignore: list[str] | None = None, limit: int = 6) -> list[str]:
    ignore_l = {i.lower() for i in (ignore or [])}
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates or []:
        k = _clean_kw(c or "")
        if not k or k.lower() in ignore_l or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
        if len(out) >= limit:
            break
    return out


def _is_number_like(tok: str) -> bool:
    if re.fullmatch(r"[\d\-–]+", tok) or re.fullmatch(r"\d{4}s?", tok):
        return True
    # 月份区间，如 April-September
    parts = re.split(r"[-–]", tok.lower())
    return len(parts) > 1 and all(p in STOPWORDS for p in parts if p)


def from_title(title: str, min_n: int = 3, max_n: int = 5) -> list[str]:
    text = strip_html(title)
    phrases: list[list[str]] = []
    for chunk in _SPLIT_RE.split(text):
        current: list[str] = []
        for tok in _TOKEN_RE.findall(chunk):
            low = tok.lower()
            if low in STOPWORDS or _is_number_like(tok) or len(tok) < 2 and not tok.isupper():
                if current:
                    phrases.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            phrases.append(current)

    # 过长短语按 4 词切开，避免整句当关键词
    split: list[list[str]] = []
    for p in phrases:
        for i in range(0, len(p), 4):
            split.append(p[i : i + 4])

    freq: dict[str, int] = {}
    degree: dict[str, int] = {}
    for p in split:
        for w in p:
            lw = w.lower()
            freq[lw] = freq.get(lw, 0) + 1
            degree[lw] = degree.get(lw, 0) + len(p) - 1
    scored: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    for idx, p in enumerate(split):
        phrase = " ".join(p)
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        score = sum((degree[w.lower()] + freq[w.lower()]) / freq[w.lower()] for w in p)
        # 首字母大写/缩写（病原、地名）略加权
        score += sum(0.5 for w in p if w[0].isupper() or w.isupper())
        scored.append((score, idx, phrase))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [s[2] for s in scored[:max_n]]
    # 不足 min_n 时，从多词短语里拆出较长的实词补齐（标题本身太短时可能仍不足）
    if len(picked) < min_n:
        have = {p.lower() for p in picked}
        singles = sorted(
            {w for p in picked if " " in p for w in p.split()
             if len(w) > 3 and w.lower() not in GENERIC and w.lower() not in have},
            key=lambda w: (-len(w), w),
        )
        for w in singles:
            if len(picked) >= min_n:
                break
            picked.append(w)
    # 保持在标题中出现的先后顺序，读起来更自然
    order = {s[2]: s[1] for s in scored}
    picked.sort(key=lambda p: order.get(p, 999))  # 补齐的单词排在后面
    return picked[:max_n]


def pick(native: list[str] | None, title: str, ignore: list[str] | None = None) -> list[str]:
    kws = from_native(native, ignore)
    return kws if kws else from_title(title)
