"""数据源注册表。

新增数据源步骤：
1. 在本目录新建模块，实现 base.Source 子类；
2. 在下面的 REGISTRY 中注册；
3. 如需在网页上使用，在 app.js 的 TABS 中加一个 tab 指向该数据源的 name。
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
]
