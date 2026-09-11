"""本地磁力库数据源：浏览 / 搜索已保存到 SQLite 的全部磁力链接。"""

from __future__ import annotations

import asyncio
import math

from db import query_items

from . import register
from .base import Item, PageResult, Source, SourceError

PAGE_SIZE = 20


def _query(page: int, keyword: str) -> PageResult:
    try:
        rows, total = query_items(page=page, keyword=keyword, page_size=PAGE_SIZE)
    except Exception as exc:
        raise SourceError(f"本地库查询失败: {exc}") from exc
    items = [Item(
        id=r["info_hash"],
        title=r["title"] or r["torrent_name"] or "（无标题）",
        magnet=r["magnet"],
        size=r["size"],
        published_at=r["published_at"],
        category=r["category"] or r["source"],
        detail_url=r["detail_url"],
        extra={"info_hash": r["info_hash"], "origin": r["source"]},
    ) for r in rows]
    return PageResult(
        items=items,
        page=page,
        total_pages=max(1, math.ceil(total / PAGE_SIZE)),
        total_items=total,
        source="local",
    )


@register
class LocalSource(Source):
    name = "local"
    label = "本地磁力库"

    async def fetch_page(self, page: int = 1, query: str = "", **kwargs) -> PageResult:
        return await asyncio.to_thread(_query, page, query.strip())
