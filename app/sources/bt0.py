"""2bt0 资源站数据源（官网 https://www.2bt0.com）。

该站是 Vue SPA，页面内容无法直接抓取（requests 只能拿到壳），
但其 JSON API 可公开访问。本模块直连其后端 API：

- 浏览模式（不输关键词）：GET /prod/api/v1/getTList?sc=<板块>&page=<页码>
    板块：1=电影 2=电视剧（与官网 /tlist/{sc}_{page}.html 一致）
    每条直接返回 zlink 磁力链接、zsize 大小、eztime 时间
- 搜索模式（输入关键词）：GET /prod/api/v1/getVideoList?sb=<关键词>
    按片名搜索影片库，返回片名/评分/年份/分类，可跳转官网详情页

API 需带固定 app_id/identity 参数（取自站点前端 JS），无需登录。
"""

from __future__ import annotations

import asyncio
import html
import time

import requests
import urllib3

from . import register
from .base import Item, PageResult, Source, SourceError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.2bt0.com"
API = BASE + "/prod/api/v1"
PAGE_SIZE = 20
RETRIES = 2

# 取自站点前端 JS 的固定参数（公开接口标识，无需登录）
APP_PARAMS = {"app_id": "83768d9ad4", "identity": "23734adac0301bccdcb107c4aa21f96c"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
    "Origin": BASE,
}

# 板块代码 → 名称（与官网 /tlist/{sc}_{page}.html 一致；3/4/5 热门榜不抓）
SECTIONS = {1: "电影", 2: "电视剧"}


def api_get(endpoint: str, params: dict) -> dict:
    """调用 2bt0 JSON API（该站 TLS 不稳定，带重试）"""
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                f"{API}/{endpoint}",
                params={**APP_PARAMS, **params},
                headers=HEADERS,
                timeout=(15, 45),
                verify=False,  # 该站证书链不完整
            )
            try:
                data = r.json()
            except ValueError as exc:
                # requests 的 JSONDecodeError 同时继承 RequestException，
                # 必须在内层单独捕获，否则会被下面的网络重试分支误判为重试
                raise SourceError(f"2bt0 接口返回非 JSON: {exc}") from exc
            if not data.get("success"):
                raise SourceError(f"接口返回错误: {data.get('message') or data}")
            return data.get("data") or {}
        except SourceError:
            raise
        except requests.exceptions.RequestException as exc:
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise SourceError(f"2bt0 接口请求失败（已重试 {RETRIES} 次）: {exc}") from exc


def movie_ref(row: dict) -> dict:
    """从一行原始数据里提取影片关联信息（片名 + 影片 id）。

    片名字段是干净的片名（zname 才是完整发布名）；
    aurl1 形如 /mv/1945336，末段数字即影片 id（对应 getVideoDetail 的 idcode）。
    """
    aurl1 = (row.get("aurl1") or "").strip().rstrip("/")
    movie_id = aurl1.split("/")[-1] if aurl1 else ""
    return {
        "movie_id": movie_id if movie_id.isdigit() else "",
        "movie_title": html.unescape((row.get("title") or "").strip()),
    }


def fetch_list(section: int, page: int) -> list[Item]:
    """抓取某板块一页种子列表（页可能为空，表示越界或站点数据空洞）。

    该站 API 的 total 字段不可信（恒 400），所以不返回总页数，
    由同步器按"连续空页"判定板块末尾。
    """
    data = api_get("getTList", {"sc": section, "page": page})
    rows = data.get("list") or []

    return [Item(
        id=str(r.get("id") or ""),
        title=r.get("zname") or r.get("title") or "（无标题）",
        magnet=(r.get("zlink") or "").strip(),
        size=r.get("zsize") or "",
        published_at=r.get("eztime") or "",
        category=SECTIONS[section],
        detail_url=BASE + r["aurl"] if r.get("aurl") else "",
        extra=movie_ref(r),
    ) for r in rows]


# getVideoDetail 返回字段 → movies 表列名
_DETAIL_MAP = {
    "title": "title", "otitle": "otitle", "alias": "alias", "years": "years",
    "class": "category", "production_area": "area", "language": "language",
    "episodes": "episodes", "long_time": "long_time", "doub_score": "doub_score",
    "doub_score_peo_num": "doub_votes", "IMDB_score_peo_num": "imdb_votes",
    "image": "image", "IMDB_number": "imdb_id", "IMDB_score": "imdb_score",
    "director": "director", "performer": "performer", "abstract": "abstract",
}


def fetch_video_detail(idcode: str) -> dict:
    """抓取一部影片的元数据（站点 getVideoDetail）。

    idcode 即种子列表里 aurl1 的末段数字（形如 /mv/1945336 → 1945336）。
    返回字典的键与 db.movies 表列名一致。
    """
    d = api_get("getVideoDetail", {"id": idcode})
    out = {"idcode": str(d.get("idcode") or idcode).strip()}
    for src, dst in _DETAIL_MAP.items():
        # 站点部分字段带 HTML 实体（如 &#39; 表示撇号），入库前解码
        out[dst] = html.unescape(str(d.get(src) or "").strip())
    # "@" 是站点表示"无评分"的占位符
    if out["doub_score"] == "@":
        out["doub_score"] = ""
    return out


def _search(keyword: str, page: int) -> PageResult:
    """按片名搜索影片库（无磁力，可跳官网详情页获取）"""
    data = api_get("getVideoList", {"sb": keyword, "page": page, "limit": PAGE_SIZE})
    rows = data.get("data") or []
    total = int(data.get("total") or len(rows))

    items = []
    for row in rows:
        title = row.get("title") or "（无标题）"
        score = row.get("doub_score") or ""
        if score not in ("", "@", "0"):  # 0/@ 表示站点无评分，不展示
            title = f"{title} [豆瓣 {score}]"
        # 详情页认的是 idcode（如 1432146），不是内部编号 id（如 10502，
        # 用它拼 /mv/ 会 404「影视资料不存在或已被删除」）
        idcode = str(row.get("idcode") or "")
        items.append(Item(
            id=idcode,
            title=title,
            magnet="",  # 影片库无磁力；磁力在种子列表（浏览模式）里
            published_at=row.get("years") or "",
            category=row.get("class") or "",
            detail_url=f"{BASE}/mv/{idcode}" if idcode else "",
        ))
    return PageResult(
        items=items,
        page=page,
        total_pages=max(1, -(-total // PAGE_SIZE)),
        total_items=total,
        source="bt0",
    )


@register
class Bt0Source(Source):
    name = "bt0"
    label = "2bt0 资源站"

    async def fetch_page(self, page: int = 1, query: str = "",
                         section: int = 1, **kwargs) -> PageResult:
        q = query.strip()
        if q:
            return await asyncio.to_thread(_search, q, page)
        sc = section if section in SECTIONS else 1
        items = await asyncio.to_thread(fetch_list, sc, page)
        return PageResult(
            # 该站 total 不可信，且存在中途短页（如某页仅 19 条），
            # 始终允许翻下一页；真正的末尾（越界页返回空）由前端提示"未找到"
            items=items,
            page=page,
            total_pages=page + 1,
            total_items=(page - 1) * PAGE_SIZE + len(items),
            source="bt0",
        )
