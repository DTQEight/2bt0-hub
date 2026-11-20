"""2bt0 资源站数据源（官网 https://www.2bt0.com）。

该站是 Vue SPA，页面内容无法直接抓取（requests 只能拿到壳），
但其 JSON API 可公开访问。本模块直连其后端 API：

- 浏览模式（不输关键词）：GET /prod/api/v1/getTList?sc=<板块>&page=<页码>
    板块：1=电影 2=电视剧（与官网 /tlist/{sc}_{page}.html 一致）
    返回 zsize 大小、eztime 时间等字段。磁力链接自 2026-11 起改为空占位
    （zlink=''），要按行 id 再调一次 recordMagnetView 才拿得到真磁力
    （见 magnet_by_view；该接口按出口 IP 每 50 次要求过一次图形验证码，
    见 gate_captcha / verify_gate，过验证后同一公网 IP 的所有客户端一起解锁）。
- 搜索模式（输入关键词）：GET /prod/api/v1/getVideoList?sb=<关键词>
    按片名搜索影片库，返回片名/评分/年份/分类，可跳转官网详情页

API 需带固定 app_id/identity 参数（取自站点前端 JS）；2026-11 中旬起拉磁力
还要求登录态，浏览器 localStorage 里的 token 通过 set_access_token 注入。
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import threading
import time

import requests
import urllib3

from . import register
from .base import Item, PageResult, Source, SourceError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://www.2bt0.com"
API = BASE + "/prod/api/v1"
PAGE_SIZE = 20
RETRIES = 2          # 网络/网关异常的重试次数
WAF_RETRIES = 4      # 撞 WAF 挑战页的重试次数（换签后偶发再被拦，需要更多耐心）

# 取自站点前端 JS 的固定参数（公开接口标识，无需登录）
APP_PARAMS = {"app_id": "83768d9ad4", "identity": "23734adac0301bccdcb107c4aa21f96c"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": BASE + "/",
    "Origin": BASE,
}

# 站点 WAF 对未通过校验的客户端返回 JS 挑战页（200 + 短 HTML，见下方
# _renew_waf_token），正常情况下无需人工干预。该 token 按请求 IP 下发，
# 所以从别的机器（如浏览器）复制的 Cookie 在容器里往往直接失效；
# 网页「同步」页仍保留手动粘贴入口，另提供「自动获取」按钮在容器内直接换取。
# Cookie 存 settings 表，优先于环境变量 BT0_COOKIE。
ENV_COOKIE = os.environ.get("BT0_COOKIE", "").strip()

# 站点按请求 IP 下发 token：容器若有多出口（多网卡 / IPv6 轮换），每次新建
# 连接可能落到不同出口 IP 上，表现就是「刚换签的 token 下一次立刻失效、
# 页面时间戳每请求都变」。用 Session 复用长连接，把同一批请求固定在同一条
# 连接（同一出口 IP）上，换签才会真正生效。
SESSION = requests.Session()
SESSION.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=8, pool_maxsize=16, max_retries=0))

logger = logging.getLogger("resource-hub.bt0")


class ViewGateRequired(SourceError):
    """匿名查看磁力满 50 次，站点要求先通过图形验证码（对公网 IP 生效）。"""


# 过验证码后站点会下发一个临时 access_token（访客令牌），前端拿到后
# 会带在后续请求上（query 参数 + Authorization 头）。只认「验证过」而不带
# 这个 token 的话，额度不会恢复——实测验证成功后立刻又被拦就是这个原因。
_ACCESS_TOKEN = ""

# 登录令牌（浏览器 localStorage 里的 token）。站点 2026-11 中旬起对未登录
# 请求直接回「请登录并开通VIP后查看磁力」，与匿名额度无关；把浏览器里的
# 这个 token 填到网页「同步」页即可，请求会带 access_token 参数 + Bearer 头。
_LOGIN_TOKEN = ""


def set_access_token(token: str) -> str:
    """设置登录令牌；传空字符串即清除。支持直接粘贴 localStorage 的原文
    （可能带引号）或 `Bearer xxx`。"""
    global _LOGIN_TOKEN
    token = (token or "").strip().strip('"').strip("'").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    _LOGIN_TOKEN = token
    return token


def get_access_token() -> str:
    return _LOGIN_TOKEN


# 站点对同一 IP 的放行名额是全局共享的（一次挑战、一次放行），并发请求
# 会让线程互相抢名额并陷入「一直被拦」。所有 API 请求串行执行。
_REQ_LOCK = threading.Lock()


def set_cookie(cookie: str, force: bool = False) -> str:
    """设置 WAF 验证 Cookie（网页保存后调用，立即生效、无需重启）。

    传空值回落到环境变量 BT0_COOKIE；force=True 时按传入值原样生效
    （「自动获取」需要真的清空旧 Cookie 去换新 token，不能被环境变量兜底）。
    """
    cookie = (cookie or "").strip()
    if not force:
        cookie = cookie or ENV_COOKIE
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

# 站点 WAF 下发 ge_js_validator_18 的 JS 挑战页（200 + HTML），里面有
# document.cookie = "ge_js_validator_18=<timestamp>@<level>@<hash>;..."
# （不同出口 IP 可能是单引号模板，这里引号和空白都当终止符）
# 这个 token 绑定到当前请求 IP，所以 NAS 上保存的 Cookie 一旦失效，
# 直接拿挑战页下发的新 token 重试即可，不需要人去浏览器过滑块。
_WAF_TOKEN_RE = re.compile(r"ge_js_validator_18=([^;\"'\s]+)")


def _persist_cookie(cookie: str) -> None:
    """把自动续签得到的 Cookie 落库，容器重启后直接复用，不用重新换取。"""
    try:
        from db import set_setting  # 延迟导入：本模块在 db 初始化前就会被 import
        set_setting("bt0.cookie", cookie)
    except Exception:  # 落库失败不影响内存里的 Cookie 继续生效
        logger.exception("自动续签的 Cookie 落库失败")


def _renew_waf_token(html_text: str) -> bool:
    """从 WAF 挑战页 HTML 里提取 ge_js_validator_18，替换到当前 Cookie。

    返回 True 表示提取到了新 token 并已写入 HEADERS（下次请求自动带上），
    同时落库，网页上能看到新掩码。
    """
    m = _WAF_TOKEN_RE.search(html_text)
    if not m:
        return False
    new_token = m.group(1)
    old = HEADERS.get("Cookie", "")
    # 替换或追加 ge_js_validator_18，保留其他 Cookie
    if "ge_js_validator_18=" in old:
        new_cookie = re.sub(r'ge_js_validator_18=[^;]*', f'ge_js_validator_18={new_token}', old)
    else:
        new_cookie = f"{old}; ge_js_validator_18={new_token}" if old else f"ge_js_validator_18={new_token}"
    HEADERS["Cookie"] = new_cookie
    logger.info("2bt0 WAF 挑战页已自动续签 ge_js_validator_18（token 尾号 %s）",
                new_token[-8:])
    _persist_cookie(new_cookie)
    return True


# 板块代码 → 名称（与官网 /tlist/{sc}_{page}.html 一致；3/4/5 热门榜不抓）
SECTIONS = {1: "电影", 2: "电视剧"}


def api_get(endpoint: str, params: dict | None = None,
            method: str = "GET", data: dict | None = None) -> dict:
    """调用 2bt0 JSON API（该站 TLS 不稳定，带重试）。

    WAF 处理：站点对同一出口 IP 几乎是「一次挑战、一次放行」的严格交替
    （实测序列 XOXOXO…，放行名额是全 IP 共享的）。被拦后用同一个 token
    立刻再发一次基本必过；若并发请求，别的线程会抢走放行名额，本线程就
    永远只看到挑战页——表现就是「换签 5 次仍被拦」。所以这里用 _REQ_LOCK
    把所有请求串行化，并在被拦时先用原 Cookie 重试，重试两次还不行才换签。
    """
    net_attempt = 0
    waf_attempt = 0
    with _REQ_LOCK:
        while True:
            try:
                token = _LOGIN_TOKEN or _ACCESS_TOKEN
                auth = {**HEADERS, "Authorization": f"Bearer {token}"} if token else HEADERS
                if method == "POST":
                    r = SESSION.post(
                        f"{API}/{endpoint}",
                        params={**APP_PARAMS, **({"access_token": token} if token else {})},
                        json=data or {},
                        headers={**auth, "Content-Type": "application/json"},
                        timeout=(15, 45),
                        verify=False,
                    )
                else:
                    r = SESSION.get(
                        f"{API}/{endpoint}",
                        params={**APP_PARAMS,
                                **({"access_token": token} if token else {}),
                                **(params or {})},
                        headers=auth,
                        timeout=(15, 45),
                        verify=False,  # 该站证书链不完整
                    )
                if "/WAF/VERIFY" in str(r.url):
                    raise SourceError(
                        "WAF 拦截：站点要求人机验证。先在网页「同步」页点「自动获取」"
                        "换一次 Cookie；仍不行再在浏览器打开 2bt0.com 过验证，"
                        "把 ge_js_validator_18 粘贴到「WAF 验证 Cookie」卡片并保存")
                # WAF JS 挑战页（200 + 短 HTML）
                if not r.text.startswith("{") and "ge_js_validator_18=" in r.text:
                    waf_attempt += 1
                    if waf_attempt > WAF_RETRIES:
                        logger.warning("WAF 连续 %d 次挑战，挑战页全文: %s",
                                       waf_attempt, r.text[:600].replace("\n", " "))
                        raise SourceError(
                            "WAF 拦截：连续被挑战且重试无效，"
                            "可在网页「同步」页点「自动获取」重试，或手动粘贴 Cookie")
                    # 已有 Cookie：先用同一个 token 立刻再试一次（通常直接放行），
                    # 不要急着重换 token——换了会把放行名额让给下一次挑战，反而更差
                    if HEADERS.get("Cookie") and waf_attempt <= 2:
                        time.sleep(0.1)
                        continue
                    if not _renew_waf_token(r.text):
                        raise SourceError("WAF 拦截：无法解析挑战页中的 ge_js_validator_18")
                    continue
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
                if net_attempt < RETRIES:
                    net_attempt += 1
                    time.sleep(1.5 * net_attempt)
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


def gate_captcha() -> dict:
    """取图形验证码（匿名查看磁力满 50 次后站点要求过这道验证）。

    返回 {key, img(data URI), ttl, length}；img 直接给前端 <img src> 用。
    """
    d = api_get("getViewGateCaptcha")
    return {
        "key": d.get("key") or "",
        "img": d.get("img") or "",
        "ttl": d.get("ttl") or 0,
        "length": d.get("length") or 0,
    }


def verify_gate(key: str, code: str) -> dict:
    """提交图形验证码。失败抛 SourceError（文案即站点返回的错误）。

    成功时站点会下发 access_token（访客令牌），后续请求必须带上它，额度才
    真正恢复——只「验证过」而不带 token，站点仍按额度满算，立刻再拦。
    """
    global _ACCESS_TOKEN
    if not key or not code:
        raise SourceError("验证码 key / code 不能为空")
    d = api_get("verifyMovieView", method="POST",
                data={"key": key, "code": (code or "").strip()})
    token = (d.get("access_token") or "").strip()
    if token:
        _ACCESS_TOKEN = token
        logger.info("2bt0 验证通过，已取得访客令牌（尾号 %s）", token[-8:])
    else:
        # 实测验证本身就是主要解锁手段（服务端按 IP 记额度），token 只是顺带；
        # 没返回也不影响继续同步
        logger.debug("2bt0 验证接口未返回 access_token，按验证通过继续")
    return d


def magnet_by_view(seed_id: str) -> str:
    """按种子 id 取磁力（站点前端「磁力」按钮走的就是这个接口）。

    每次调用消耗 1 次匿名查看额度（站点按出口 IP 计 50 次），额度用完后
    返回 need_view_verify，需先过图形验证码（见 gate_captcha / verify_gate）。
    验证对同一公网 IP 生效，所以浏览器上过验证后，容器这边也会自动解锁。
    """
    d = api_get("recordMagnetView", {"id": seed_id})
    link = (d.get("zlink") or "").strip()
    if link.startswith("magnet:"):
        return link
    if d.get("need_view_verify") or d.get("view_limit"):
        raise ViewGateRequired(
            f"匿名查看磁力已达上限（{d.get('view_limit') or 50} 次），需要输入图形验证码")
    return ""


def test_magnet() -> dict:
    """用当前令牌真实取一条磁力，给网页「测试」按钮用。

    直接拿电影第 1 页第一条，不消耗额外额度（本来也要抓）。
    抛 SourceError 时文案即站点返回的拒绝原因（如「请登录并开通VIP」）。
    """
    data = api_get("getTList", {"sc": 1, "page": 1})
    rows = data.get("list") or []
    if not rows:
        raise SourceError("列表接口返回空，无法测试磁力")
    row = rows[0]
    link = magnet_by_view(str(row.get("id") or ""))
    if not link:
        raise SourceError("接口未返回磁力链接（可能仍被站点限制）")
    return {"id": str(row.get("id") or ""),
            "title": str(row.get("zname") or row.get("title") or "")[:60],
            "magnet": link[:48] + "…"}


def fetch_list(section: int, page: int) -> list[Item]:
    """抓取某板块一页种子列表（页可能为空，表示越界或站点数据空洞）。

    该站 API 的 total 字段不可信（恒 400），所以不返回总页数，
    由同步器按"连续空页"判定板块末尾。

    2026-11 起列表只给 zlink='' 占位，磁力要按行 id 再调一次
    recordMagnetView 才拿得到；撞上图形验证码时抛 ViewGateRequired，
    由调用方中断本轮（验证后重跑即可，已入库的不会重复消耗额度）。
    """
    data = api_get("getTList", {"sc": section, "page": page})
    rows = data.get("list") or []

    items: list[Item] = []
    for r in rows:
        film = movie_ref(r)
        title = r.get("zname") or r.get("title") or "（无标题）"
        magnet = (r.get("zlink") or "").strip()
        if not magnet.startswith("magnet:"):
            magnet = magnet_by_view(str(r.get("id") or ""))
        if not magnet:
            continue
        items.append(Item(
            id=str(r.get("id") or ""),
            title=title,
            magnet=magnet,
            size=r.get("zsize") or "",
            published_at=r.get("eztime") or "",
            category=SECTIONS[section],
            detail_url=BASE + r["aurl"] if r.get("aurl") else "",
            extra=film,
        ))
    return items

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
