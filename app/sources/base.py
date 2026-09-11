"""数据源适配器接口。

接入一个新站点：新建一个模块实现 Source 子类，然后在 sources/__init__.py
的 REGISTRY 里注册即可，无需改动 web 层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


@dataclass
class Item:
    """一条资源记录。"""

    id: str
    title: str
    magnet: str = ""
    size: str = ""
    published_at: str = ""
    category: str = ""
    detail_url: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageResult:
    """一页抓取结果。"""

    items: list[Item]
    page: int
    total_pages: int
    total_items: int
    source: str

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "page": self.page,
            "total_pages": self.total_pages,
            "total_items": self.total_items,
            "source": self.source,
        }


class Source(ABC):
    """资源站点适配器基类。"""

    name: str = "base"
    label: str = "未命名数据源"

    @abstractmethod
    async def fetch_page(self, page: int = 1, query: str = "", **kwargs) -> PageResult:
        """抓取一页资源列表。

        :param page: 页码，从 1 开始
        :param query: 搜索关键词，空字符串表示不做筛选
        :param kwargs: 数据源自定义参数（如板块代码）
        """
        raise NotImplementedError

    async def get_magnet(self, item_id: str) -> dict:
        """按需获取单条资源的磁力链接（子类可选实现）。

        :param item_id: Item.id
        :return: 至少包含 magnet 字段，可附带 name / info_hash / trackers 等
        """
        raise SourceError(f"数据源 {self.name} 不支持按需获取磁力链接")


class SourceError(RuntimeError):
    """数据源抓取失败。"""
