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
import os
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

# 站点 WAF 会对未通过人机验证的客户端返回 307 跳验证页。浏览器里过一次滑块后，
# WAF 下发验证 Cookie（如 ge_wc_18=...，实测不绑定 UA/TLS 指纹，普通 HTTP 客户端可复用）。
# Cookie 会过期：过期后请求重新被 307，同步日志报「WAF 拦截」，在网页「同步」页
# 粘贴新 Cookie 即可热生效（存 settings 表，优先于环境变量 BT0_COOKIE）。
ENV_COOKIE = os.environ.get("BT0_COOKIE", "").strip()


def set_cookie(cookie: str) -> str:
    """设置 WAF 验证 Cookie（网页保存后调用，立即生效、无需重启）。

    传空值回落到环境变量 BT0_COOKIE；两者都为空则不带 Cookie 头。
    """
    cookie = (cookie or "").strip() or ENV_COOKIE
    if cookie:
        HEADERS["Cookie"] = cookie
    else:
        HEADERS.pop("Cookie", None)
    return cookie


def get_cookie() -> str:
    """当前生效的 WAF Cookie（可能来自网页设置或环境变量）"""
    return HEADERS.get("Cookie", "")


def test_connection(section: int = 1, page: int = 1) -> dict:
    """用当前 Cookie 真实请求一次列表接口，判断是否已通过 WAF。

    供网页「同步」页的「测试」按钮调用：成功返回条数/耗时/样例标题，
    被 WAF 拦截时抛 SourceError（错误文案里已写明怎么换 Cookie）。
    """
    t0 = time.monotonic()
    data = api_get("getTList", {"sc": section, "page": page})
    rows = data.get("list") or []
    sample = ""
    if rows:
        sample = str(rows[0].get("title") or rows[0].get("zname") or "")[:40]
    return {
        "count": len(rows),
        "latency_ms": int((time.monotonic() - t0) * 1000),
        "sample": sample,
    }


set_cookie("")  # 启动时先用环境变量兜底，网页设置过的值由 main.py 启动时覆盖

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
            if "/WAF/VERIFY" in str(r.url):
                # requests 会跟随 307 重定向，所以看最终 URL 是否落在验证页。
                # WAF 拦截重试也不会过，直接给出可操作的错误、不进重试
                raise SourceError(
                    "WAF 拦截：站点要求人机验证。在浏览器打开 2bt0.com 过一次滑块，"
                    "把新 Cookie 粘贴到网页「同步」页的「WAF 验证 Cookie」卡片"
                    "（或更新 BT0_COOKIE 环境变量），保存后可继续同步")
            try:
                data = r.json()
            except ValueError as exc:
                # 非 JSON 多为网关/CDN 偶发错误页（HTML），与网络抖动一样值得重试。
                # requests.JSONDecodeError 同时继承 RequestException，须先在内层
                # 捕获并转成普通 RequestException，才能落进下面的重试分支，
                # 而不是带着原始解码错误直接漏出去
                raise requests.RequestException(
                    f"2bt0 接口返回非 JSON: {exc}") from exc
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
    影片链接形如 /mv/1945336，末段数字即影片 id（对应 getVideoDetail 的 idcode）。
    站点 2026-11 起把该字段从 aurl1 改名为 mv（同时 aurl 改成 /tr/<id>.html
    的种子页），这里优先取 mv，并用 aurl1 兜底兼容老响应。
    """
    path = (row.get("mv") or row.get("aurl1") or "").strip().rstrip("/")
    movie_id = path.split("/")[-1] if path else ""
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
# tags / definition 是站点影片库筛选用的「影视标签」与「资源画质」（逗号分隔），
# 详情页接口里就有，取下来存库，本地筛选条才能按这两项过滤
# 站点 edit=编剧、updated_at=站点侧更新时间（区别于本库的 fetched_at）、
# type=板块标记（1=电影 2=电视剧）
_DETAIL_MAP = {
    "title": "title", "otitle": "otitle", "alias": "alias", "years": "years",
    "class": "category", "production_area": "area", "language": "language",
    "episodes": "episodes", "long_time": "long_time", "doub_score": "doub_score",
    "doub_score_peo_num": "doub_votes", "IMDB_score_peo_num": "imdb_votes",
    "image": "image", "IMDB_number": "imdb_id", "IMDB_score": "imdb_score",
    "director": "director", "performer": "performer", "abstract": "abstract",
    "tags": "tags", "definition": "definition", "release": "release",
    "edit": "writer", "updated_at": "site_updated_at", "type": "video_type",
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
