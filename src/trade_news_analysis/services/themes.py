"""Conservative canonicalization for recurring investment themes."""

from __future__ import annotations

import re

THEME_ALIASES = {
    "ai芯片": "AI计算芯片",
    "ai算力芯片": "AI计算芯片",
    "ai加速计算芯片": "AI计算芯片",
    "ai数据中心gpu": "数据中心GPU",
    "ai算力资本开支": "AI数据中心资本开支",
    "ai基础设施资本开支": "AI数据中心资本开支",
    "ai算力基础设施": "AI数据中心基础设施",
    "数据中心基础设施": "AI数据中心基础设施",
}


def theme_key(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.casefold()))


def canonicalize_theme(value: str) -> str:
    normalized = " ".join(value.strip().split())
    return THEME_ALIASES.get(theme_key(normalized), normalized)
