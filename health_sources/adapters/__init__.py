"""适配器注册表：每个来源一个适配器，负责把原始数据对到统一字段。
Europe PMC 不再是来源，只在 enrich 步骤里按 DOI 回查（见 health_sources/enrich.py）。"""
from . import ccdc, mmwr, rss, who_don

ADAPTERS = {
    "rss": rss,
    "ccdc": ccdc,
    "mmwr": mmwr,
    "who_don": who_don,
}
