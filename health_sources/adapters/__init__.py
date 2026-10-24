"""适配器注册表：每个来源一个适配器，负责把原始数据对到统一字段。"""
from . import europepmc, mmwr, rss, who_don

ADAPTERS = {
    "rss": rss,
    "mmwr": mmwr,
    "who_don": who_don,
    "europepmc": europepmc,
}
