"""数据源注册表。

新增数据源步骤：
1. 在本目录新建模块，实现 base.Source 子类；
2. 在下面的 REGISTRY 中注册；
3. 在 .env 里把 SOURCE 改成对应的 name（或运行时通过网页顶部下拉框切换）。
"""

from __future__ import annotations

from .base import Item, PageResult, Source, SourceError

REGISTRY: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    if not cls.name or cls.name == "base":
        raise ValueError(f"数据源 {cls.__name__} 缺少有效的 name")
    REGISTRY[cls.name] = cls
    return cls


def get_source(name: str) -> Source:
    if name not in REGISTRY:
        raise SourceError(f"未知数据源: {name}，可选: {', '.join(REGISTRY) or '无'}")
    return REGISTRY[name]()


def list_sources() -> list[dict[str, str]]:
    return [{"name": n, "label": c.label} for n, c in REGISTRY.items()]


from . import bt0  # noqa: E402,F401  触发注册
from . import local  # noqa: E402,F401  触发注册

__all__ = [
    "Item",
    "PageResult",
    "Source",
    "SourceError",
    "REGISTRY",
    "register",
    "get_source",
    "list_sources",
]
