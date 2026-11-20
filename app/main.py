"""Web 服务：2bt0 磁力资源库 API + 前端页面 + 后台全量同步。"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import math
import os
import re
import requests
import threading
import time
import urllib3
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse)
from fastapi.staticfiles import StaticFiles

import admin_auth
from api_auth import (api_key_for_admin, get_api_key, is_public_api_path,
                      require_api_key)
from db import (filter_options, get_movie, get_setting, get_sync_done,
                get_sync_progress, init_db, magnet_by_hash, magnets_for_movie,
                query_groups, query_items, quality_counts, search_items,
                set_setting, upsert_items)
import quality
from movies import movie_detail_manager
from posters import migrate_to_shards, resolve as resolve_poster
from scheduler import get_schedule, next_run_at, set_schedule, start_scheduler
from sources import SourceError, get_source
from sources.bt0 import (SECTIONS, gate_captcha, get_access_token, get_cookie,
                         set_access_token, set_cookie, test_connection,
                         test_magnet, verify_gate)
from sync import sync_manager
import cloud115
import transfer_history
import wecom
from crypto_utils import encrypt

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
POSTER_DIR = DATA_DIR / "posters"  # 海报本地缓存（图床下载，见 posters.py）

for sub in ("logs", "db", "tmp", "posters"):
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        # 轮转日志：容器长期运行时避免 app.log 无限增长（/api/logs 只读当前文件）
        logging.handlers.RotatingFileHandler(
            DATA_DIR / "logs" / "app.log", maxBytes=5 * 1024 * 1024,
            backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("resource-hub")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

init_db()  # 本地磁力库建表
# 对外 API 密钥：首次启动生成并打印一次到日志（之后从库里读，不重复打印）
get_api_key()
# 后台管理账号：首次启动创建（密码取 ADMIN_PASSWORD，未设置则随机生成并打印）
admin_auth.ensure_admin()


def _auto_resume() -> None:
    """容器启动：把存在断点的板块依次自动续抓（电影跑完接着跑电视剧）。

    同步器是单实例（一次只能跑一个板块），所以在后台线程里串行接力，
    否则只有最小板块号的那个能恢复，另一个断点会一直闲置；
    手动停止其中一个板块后，剩余板块不再自动启动。
    """
    pending = get_sync_progress("bt0")
    for sec in sorted(pending):
        if sync_manager._stop.is_set():  # 上一个板块是被手动停掉的
            logger.info("自动续抓已手动停止，剩余板块不再自动启动")
            break
        try:
            sync_manager.start(sec, start_page=pending[sec] + 1, mode="resume")
            logger.info("检测到未完成断点，自动续抓：%s（板块 %d）从第 %d 页",
                        SECTIONS[sec], sec, pending[sec] + 1)
        except Exception:
            logger.exception("自动续抓启动失败（板块 %d），可稍后手动点击同步按钮", sec)
            continue
        while sync_manager.state["running"]:
            time.sleep(2)


# 容器启动时若存在未完成的同步断点，后台线程自动续抓（重建容器也不丢进度）
if get_sync_progress("bt0"):
    threading.Thread(target=_auto_resume, name="auto-resume", daemon=True).start()

# 海报分桶迁移：历史版本把 7 万+ 张海报全放 posters 根目录，改为哈希分桶
# 子目录（每桶约一万张）。迁移在后台进行，期间 find/路由均有根目录兜底
threading.Thread(target=migrate_to_shards, name="poster-migrate", daemon=True).start()

start_scheduler()  # 每日定时增量更新（电影 + 电视剧）
# WAF Cookie：网页「同步」页保存过的值优先，未设置时回落到环境变量 BT0_COOKIE
set_cookie(get_setting("bt0.cookie", ""))
# 登录令牌：站点 2026-11 起对未登录请求拒绝给磁力，网页填过就带上
set_access_token(get_setting("bt0.token", ""))

app = FastAPI(title="2bt0 资源库", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def static_no_cache(request, call_next):
    """静态资源禁用启发式缓存：不发 Cache-Control 时浏览器会把旧 JS
    一直当新的用（更新镜像后页面行为不变就是这个原因）。
    no-cache 仍带 ETag 协商，命中时回 304，代价极小。"""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.middleware("http")
async def api_key_guard(request, call_next):
    """鉴权分两层：

    1. 后台管理接口（同步/日志/115/企微/定时…）只认登录 Cookie（见 admin_auth），
       不认 X-API-Key——那把钥匙连公网访客都能从 /api/apikey 换到。
    2. 其余 /api/* 走全站 API 密钥：/api/health（容器 healthcheck 依赖）、
       /api/apikey（查看密钥本身）、/api/docs 与 /api/openapi.json（文档）豁免。

    用中间件而不是逐个路由加依赖：新增接口自动受保护，不会有人漏加。
    网页自己不豁免，而是由 app.js 自动带上密钥 / Cookie（见 static/app.js 顶部）。
    """
    path = request.url.path
    if admin_auth.is_admin_path(path):
        if not admin_auth.check_session(request):
            return JSONResponse({"detail": "需要登录后台管理"}, status_code=401)
        return await call_next(request)
    if path.startswith("/api/") and not is_public_api_path(path) \
            and path not in admin_auth.PUBLIC_ADMIN_PATHS:
        try:
            require_api_key(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


# ---- 后台管理登录 ----

@app.get("/api/admin/session")
async def admin_session(request: Request) -> dict:
    """当前浏览器是否已登录（前端载入时探一次，决定是否显示后台 Tab）"""
    logged_in = admin_auth.check_session(request)
    return {"logged_in": logged_in,
            "username": admin_auth.get_username() if logged_in else ""}


@app.post("/api/admin/login")
async def admin_login(body: dict, request: Request, response: Response) -> dict:
    user = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ok, msg = admin_auth.verify_login(user, password)
    host = request.client.host if request.client else "?"
    if not ok:
        time.sleep(0.2)  # 轻微延迟，压低暴力尝试速率
        logger.warning("后台管理登录失败（来源 %s）", host)
        raise HTTPException(status_code=401, detail=msg)
    admin_auth.attach_cookie(response, admin_auth.create_session())
    logger.info("后台管理登录成功（来源 %s）", host)
    return {"success": True}


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict:
    admin_auth.drop_session(request.cookies.get(admin_auth.COOKIE_NAME, ""))
    admin_auth.clear_cookie(response)
    return {"success": True}


@app.post("/api/admin/password")
async def admin_change_password(body: dict) -> dict:
    ok, msg = admin_auth.change_password(str(body.get("old") or ""),
                                         str(body.get("new") or ""))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


@app.post("/api/admin/username")
async def admin_change_username(body: dict) -> dict:
    """改后台账号名：要带当前密码，防止会话被劫持后直接把主人锁在门外。"""
    ok, msg = admin_auth.change_username(str(body.get("password") or ""),
                                         str(body.get("new") or ""))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg,
            "username": admin_auth.get_username()}


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR)}


@app.get("/api/items")
async def items(
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=200),
    source: str = Query("bt0", max_length=50),
    sc: int = Query(1, ge=1, le=2, description="板块：1=电影 2=电视剧"),
    category: str = Query("", max_length=20, description="本地库分类过滤，空为全部"),
    movie_id: str = Query("", max_length=30, description="本地库：只看某部影片的版本"),
    quality: str = Query("", max_length=20,
                         description="本地库：只看某画质桶（见 /api/movie/{id}/versions/qualities）"),
) -> dict:
    name = source
    try:
        result = await get_source(name).fetch_page(
            page=page, query=q.strip(), section=sc, category=category.strip(),
            movie_id=movie_id.strip(), quality=quality.strip())
    except SourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # 抓取失败统一转成 502，避免把堆栈暴露给前端
        logger.exception("数据源 %s 抓取失败", name)
        raise HTTPException(status_code=502, detail=f"抓取失败: {exc}") from exc
    # 浏览到的磁力链接全量入库（本地库除外，避免自写自读）
    if name != "local":
        try:
            await asyncio.to_thread(upsert_items, name, result.items)
        except Exception:
            logger.exception("磁力入库失败（不影响返回）")
    return result.to_dict()


# ---- 2bt0 全量同步 ----

@app.post("/api/sync/start")
async def sync_start(body: dict) -> dict:
    """启动同步。body: {section: 1|2, mode?: "full"|"update", start_page?: int, workers?: 4}

    mode 不传时自动选择：有断点→续抓；已全量完成→增量更新；否则→全量。
    mode="update" 要求该板块已完成全量同步。
    """
    try:
        section = int(body.get("section") or 0)
        workers = max(1, min(8, int(body.get("workers") or 4)))
        start_page = int(body["start_page"]) if body.get("start_page") else None
        mode = body.get("mode")
        sync_manager.start(section, start_page=start_page, workers=workers, mode=mode)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return sync_manager.status()


@app.get("/api/sync/status")
async def sync_status() -> dict:
    return sync_manager.status()


@app.post("/api/sync/stop")
async def sync_stop() -> dict:
    sync_manager.stop()
    return sync_manager.status()


# ---- 影片：按片名分组浏览 + 影片详情 ----

# 海报墙每页 24 部：宽屏 8 列正好铺满 3 行
GROUPS_PAGE_SIZE = 24

# 本地海报文件名白名单（文件名由 posters.py 生成；不许出现路径分隔符，防目录穿越）
_POSTER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.(?:jpg|jpeg|png|webp|gif|avif)$")

# 显式给出 MIME：Python 的 mimetypes 依赖系统 mime 库，容器里认不出 webp/avif
# 时会退回 text/plain，浏览器可能拒绝渲染
_POSTER_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                 "webp": "image/webp", "gif": "image/gif", "avif": "image/avif"}


def _local_image(rec: dict) -> dict:
    """海报优先用本地缓存（/posters/xxx.webp），没缓存或文件已丢则回落到图床原地址。

    海报已分桶存放在 posters/00~09 子目录，文件定位统一走
    posters.resolve（桶目录优先、根目录兜底）。"""
    name = str(rec.pop("poster_path", "") or "")
    if name and resolve_poster(name) is not None:
        rec["image"] = f"/posters/{name}"
    return rec


@app.get("/posters/{name}")
async def poster(name: str) -> FileResponse:
    """本地缓存的海报图（拉影片详情时从图床下载，见 posters.py）。

    文件实际存放在 posters/00~09 分桶子目录里，由 resolve() 按文件名
    哈希还原路径；URL 格式 /posters/{idcode}.{ext} 保持历史不变。
    """
    if not _POSTER_RE.match(name):
        raise HTTPException(status_code=404, detail="海报不存在")
    path = resolve_poster(name)
    if path is None:
        raise HTTPException(status_code=404, detail="海报不存在")
    # 海报内容不会变，让浏览器长期缓存，省掉重复下载
    return FileResponse(path,
                        media_type=_POSTER_TYPES.get(name.rsplit(".", 1)[-1], "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=2592000"})


@app.get("/api/groups")
async def groups(
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=200),
    person: str = Query("", max_length=100,
                        description="按演职人员精确筛选（演员/导演/编剧任一角色）"),
    category: str = Query("", max_length=20, description="分类过滤：电影/电视剧，空为全部"),
    sort: str = Query("last", max_length=10,
                      description="排序：last=最新入库/score=豆瓣评分/votes=评分人数/"
                                  "years=年份/versions=版本数"),
    ftype: str = Query("", max_length=30, description="筛选：影视类型"),
    farea: str = Query("", max_length=30, description="筛选：制片地区"),
    fyears: str = Query("", max_length=20, description="筛选：上映年份"),
    fquality: str = Query("", max_length=30, description="筛选：资源画质"),
    ftag: str = Query("", max_length=30, description="筛选：影视标签"),
    votes_min: int = Query(0, ge=0, le=50_000_000, description="筛选：评分人数下限"),
    score_min: float = Query(0, ge=0, le=10, description="筛选：豆瓣评分下限"),
    score_max: float = Query(10, ge=0, le=10, description="筛选：豆瓣评分上限"),
    imdb_only: bool = Query(False, description="筛选：仅看有 IMDb 评分的影片"),
) -> dict:
    """按影片分组浏览本地库：每组＝一部影片及其版本数（可带筛选条条件）"""
    filters = {
        "ftype": ftype.strip(), "farea": farea.strip(), "fyears": fyears.strip(),
        "fquality": fquality.strip(), "ftag": ftag.strip(),
        "votes_min": votes_min, "imdb_only": imdb_only,
    }
    # 评分区间只在偏离默认值时才作为条件，避免把库里没有评分的影片全过滤掉
    if score_min > 0:
        filters["score_min"] = score_min
    if score_max < 10:
        filters["score_max"] = score_max
    try:
        rows, total = await asyncio.to_thread(
            query_groups, page, q.strip(), category.strip(), GROUPS_PAGE_SIZE, sort,
            filters, person.strip())
    except Exception as exc:
        logger.exception("分组查询失败")
        raise HTTPException(status_code=502, detail=f"分组查询失败: {exc}") from exc
    return {
        "groups": [_local_image(dict(r)) for r in rows],
        "page": page,
        "total_pages": max(1, math.ceil(total / GROUPS_PAGE_SIZE)),
        "total_groups": total,
    }


@app.get("/api/filters")
async def filters(category: str = Query("", max_length=20,
                                        description="分类：电影/电视剧，空为全部")) -> dict:
    """本地库筛选条的可选项（分类参考主站影片库筛选，只列出库里确实有的标签）"""
    return await asyncio.to_thread(filter_options, category.strip())


@app.get("/api/movie/{idcode}")
async def movie_detail(idcode: str) -> dict:
    """影片详情（站点 getVideoDetail 的元数据）；未拉取过时 movie 为 null"""
    movie = await asyncio.to_thread(get_movie, idcode)
    return {"movie": _local_image(dict(movie)) if movie else None}


@app.get("/api/movie/{idcode}/versions/qualities")
async def movie_version_qualities(idcode: str) -> dict:
    """该影片各画质桶的版本数（详情页「磁力链接」筛选条用）。

    只统计这部片子的种子，按需分类、不落库，只返回 count>0 的桶。
    """
    counts = await asyncio.to_thread(quality_counts, idcode)
    return {"qualities": [{"name": k, "count": v} for k, v in counts.items()],
            "total": sum(counts.values())}


# ---- 对外 API：按影片取磁力（需 API 密钥）----

@app.get("/api/apikey")
async def apikey_view(request: Request) -> dict:
    """查看调用 /api/magnets 用的密钥（仅局域网/本机可读）。"""
    return api_key_for_admin(request)


@app.get("/api/magnets", dependencies=[Depends(require_api_key)])
async def api_magnets(
    movie_id: str = Query("", max_length=30, description="影片 idcode（豆瓣 subject id）"),
    info_hash: str = Query("", max_length=40, description="直接按种子 info_hash 取单条"),
    quality_bucket: str = Query("", alias="quality", max_length=20,
                                description="只取某画质桶，如 1080P蓝光"),
    limit: int = Query(200, ge=1, le=1000, description="最多返回条数"),
    offset: int = Query(0, ge=0, description="跳过前 N 条（配合 limit 分页）"),
    format: str = Query("json", pattern="^(json|text|magnets)$",
                        description="json=带元数据；text=一行一条磁力；magnets=只输出 magnet 链接"),
) -> Response:
    """按影片或 info_hash 取磁力链接（自动化/脚本用）。

    调用方式：
        curl -H "X-API-Key: <密钥>" "http://<host>:8000/api/magnets?movie_id=35627661"
        curl -H "X-API-Key: <密钥>" "...&quality=1080P蓝光&format=text"

    返回（format=json）：
        {"movie_id": ..., "movie_title": ..., "total": 12, "returned": 12,
         "magnets": [{"info_hash","magnet","title","size","published_at",
                      "category","detail_url","quality"}]}

    format=text / magnets 时返回纯文本，一行一条磁力链接，便于直接喂给下载器；
    total 等元数据通过响应头 X-Total-Count / X-Returned-Count 返回。
    """
    if not movie_id and not info_hash:
        raise HTTPException(status_code=400, detail="需指定 movie_id 或 info_hash")
    if quality_bucket and quality_bucket not in quality.BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"未知画质桶 {quality_bucket}，可选：{' / '.join(quality.BUCKETS)}")

    if info_hash:
        # 单条：info_hash 唯一，按主键等值查询
        rows, total = await asyncio.to_thread(_magnets_for_hash, info_hash, quality_bucket)
        movie = None
    else:
        rows, total = await asyncio.to_thread(
            magnets_for_movie, movie_id, quality_bucket, limit, offset)
        movie = await asyncio.to_thread(get_movie, movie_id)

    items = [{
        "info_hash": r["info_hash"], "magnet": r["magnet"], "title": r["title"],
        "size": r["size"], "published_at": r["published_at"],
        "category": r["category"], "detail_url": r["detail_url"],
        "quality": quality.classify(r["title"]),
    } for r in rows if r["magnet"]]

    if format in ("text", "magnets"):
        body = "\n".join(i["magnet"] for i in items)
        if format == "text" and movie:
            # 首行注释掉，纯 magnets 格式不加，方便直接管道给下载器
            body = f"# {movie['title'] or movie_id} ({len(items)}/{total} 条)\n" + body
        return PlainTextResponse(
            body + ("\n" if body else ""),
            headers={"X-Total-Count": str(total), "X-Returned-Count": str(len(items))})

    return {
        "movie_id": movie_id,
        "movie_title": (movie["title"] if movie else "") or (items[0]["title"] if items else ""),
        "quality": quality_bucket,
        "total": total,
        "returned": len(items),
        "magnets": items,
    }


def _magnets_for_hash(info_hash: str, quality_bucket: str = ""):
    """按 info_hash 精确取单条磁力；带 quality 时要求该条也属于该桶。"""
    row = magnet_by_hash(info_hash)
    if not row:
        return [], 0
    if quality_bucket and quality.classify(row["title"]) != quality_bucket:
        return [], 0
    return [row], 1


# ---- 影片详情批量拉取 ----

@app.post("/api/movies/start")
async def movies_start(body: dict) -> dict:
    """启动影片详情拉取。body: {section?: 1|2}，不传则拉全部板块的待拉取影片"""
    try:
        section = int(body.get("section") or 0)
        movie_detail_manager.start(section or None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await asyncio.to_thread(movie_detail_manager.status)


@app.get("/api/movies/status")
async def movies_status() -> dict:
    return await asyncio.to_thread(movie_detail_manager.status)


@app.post("/api/movies/stop")
async def movies_stop() -> dict:
    movie_detail_manager.stop()
    return await asyncio.to_thread(movie_detail_manager.status)


def _schedule_payload(cfg: dict) -> dict:
    nxt = next_run_at(cfg)
    return {**cfg, "next_run": nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""}


@app.get("/api/schedule")
async def schedule_get() -> dict:
    """每日定时增量更新的配置与下次执行时间"""
    return _schedule_payload(get_schedule())


@app.post("/api/schedule")
async def schedule_set(body: dict) -> dict:
    """修改定时任务。body: {enabled?: bool, hours?: [0-23, ...]}"""
    hours = body.get("hours", body.get("hour"))  # hour 兼容旧前端
    try:
        cfg = set_schedule(enabled=body.get("enabled"), hours=hours)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"参数无效：{exc}") from exc
    logger.info("定时任务配置已更新：%s，每天 %s",
                "已开启" if cfg["enabled"] else "已关闭",
                "、".join(f"{h:02d}:00" for h in cfg["hours"]))
    return _schedule_payload(cfg)


def _mask_cookie(cookie: str) -> str:
    """Cookie 只回掩码，网页接口不暴露完整验证串"""
    if not cookie:
        return ""
    return (cookie[:6] + "****" + cookie[-6:]) if len(cookie) > 20 else "****"


@app.get("/api/bt0cookie")
async def bt0_cookie_view() -> dict:
    """当前生效的 2bt0 WAF 验证 Cookie（只回掩码，不回明文）"""
    active = get_cookie()
    saved = get_setting("bt0.cookie", "")
    source = "网页设置" if saved else ("环境变量" if active else "")
    return {"has_cookie": bool(active), "cookie_masked": _mask_cookie(active), "source": source}


@app.post("/api/bt0cookie")
async def bt0_cookie_set(body: dict) -> dict:
    """保存 2bt0 WAF 验证 Cookie（存 settings 表），立即生效、无需重启"""
    cookie = (body.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    set_setting("bt0.cookie", cookie)
    set_cookie(cookie)
    logger.info("2bt0 WAF Cookie 已更新（网页设置，立即生效）")
    return {"success": True, "message": "Cookie 已保存并立即生效"}


@app.post("/api/bt0cookie/test")
async def bt0_cookie_test(body: dict) -> dict:
    """用当前 Cookie 真实请求一次列表接口，验证是否已通过 WAF。

    传 cookie 时只用于本次测试（测完还原，不会覆盖已保存的值），
    方便先验证再保存。
    """
    prev = get_cookie()
    cookie = (body.get("cookie") or "").strip()
    if cookie:
        set_cookie(cookie)
    try:
        info = await asyncio.to_thread(test_connection, 1, 1)
    except SourceError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # 网络/解析异常也要给前端可读结果
        return {"success": False, "message": f"测试失败: {exc}"}
    finally:
        if cookie:
            set_cookie(prev)  # 测试不改变当前生效的 Cookie
    return {
        "success": True,
        "count": info["count"],
        "latency_ms": info["latency_ms"],
        "sample": info["sample"],
        "message": (f"抓取正常：{info['count']} 条 / {info['latency_ms']}ms"
                    + (f"，首条「{info['sample']}」" if info["sample"] else "")),
    }


@app.post("/api/bt0cookie/renew")
async def bt0_cookie_renew() -> dict:
    """在容器内自动获取新的 WAF Cookie（NAS 上不必再去浏览器手动复制）。

    清空旧 Cookie 请求一次站点列表：站点回 JS 挑战页，从中取出带当前
    请求 IP 的新 token 后重试，验证通过即落库；失败则回滚原来的 Cookie。
    """
    prev = get_cookie()
    try:
        set_cookie("", force=True)
        info = await asyncio.to_thread(test_connection, 1, 1)
    except SourceError as exc:
        set_cookie(prev, force=True)
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # 网络/解析异常也要给前端可读结果
        set_cookie(prev, force=True)
        return {"success": False, "message": f"自动获取失败: {exc}"}
    cookie = get_cookie()
    if not cookie:
        set_cookie(prev, force=True)
        return {"success": False, "message": "站点未下发验证 Cookie"}
    set_setting("bt0.cookie", cookie)
    logger.info("2bt0 WAF Cookie 已自动获取并保存（%s）", _mask_cookie(cookie))
    return {
        "success": True,
        "cookie_masked": _mask_cookie(cookie),
        "count": info["count"],
        "latency_ms": info["latency_ms"],
        "message": (f"已自动获取新 Cookie 并验证通过：{info['count']} 条 / "
                    f"{info['latency_ms']}ms"),
    }


@app.get("/api/bt0token")
async def bt0_token_view() -> dict:
    """当前登录令牌状态（只回掩码）。站点 2026-11 起匿名拉磁力会被拒。"""
    token = get_access_token()
    return {"has_token": bool(token),
            "token_masked": _mask_cookie(token) if token else "",
            "source": "网页设置" if get_setting("bt0.token", "") else ""}


@app.post("/api/bt0token")
async def bt0_token_set(body: dict) -> dict:
    """保存登录令牌（浏览器 localStorage 里的 token），立即生效、无需重启。"""
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token 不能为空")
    set_setting("bt0.token", token)
    saved = set_access_token(token)
    logger.info("2bt0 登录 Token 已更新（%s）", _mask_cookie(saved))
    return {"success": True, "message": "登录 Token 已保存并立即生效"}


@app.post("/api/bt0token/test")
async def bt0_token_test(body: dict) -> dict:
    """用当前（或传入的）令牌真实取一条磁力，确认站点认可登录态。

    传 token 时只用于本次测试，测完还原，不会覆盖已保存的值。
    """
    prev = get_access_token()
    token = (body.get("token") or "").strip()
    if token:
        set_access_token(token)
    try:
        info = await asyncio.to_thread(test_magnet)
    except SourceError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:  # 网络/解析异常也要给前端可读结果
        return {"success": False, "message": f"测试失败: {exc}"}
    finally:
        if token:
            set_access_token(prev)
    return {"success": True, **info,
            "message": f"取到磁力：{info['title']}"}


@app.get("/api/bt0gate")
async def bt0_gate_captcha() -> dict:
    """取一张图形验证码（匿名查看磁力满 50 次后站点要求过验证）。

    验证结果对出口公网 IP 生效，所以在这里过一次，容器（NAS）和浏览器
    都会一起解锁；不必登录、也不需要 VIP。
    """
    try:
        return await asyncio.to_thread(gate_captcha)
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/bt0gate")
async def bt0_gate_verify(body: dict) -> dict:
    """提交图形验证码。body: {key, code}；成功后同步额度恢复 50 次。"""
    key = (body.get("key") or "").strip()
    code = (body.get("code") or "").strip()
    try:
        await asyncio.to_thread(verify_gate, key, code)
    except SourceError as exc:
        return {"success": False, "message": str(exc)}
    except Exception as exc:
        return {"success": False, "message": f"验证失败: {exc}"}
    logger.info("2bt0 图形验证码已通过（匿名查看额度已恢复）")
    sync_manager.state["gate_key"] = ""
    return _resume_after_gate()


def _resume_after_gate() -> dict:
    """验证通过后：若同步正卡在验证码上，自动从那一页续抓。

    网页输入与企业微信回复共用这一条路径。手动推送验证码（同步并未卡住）
    没有 gate_page，此时按增量更新从第 1 页起跑，避免验证完还要手动点同步。
    """
    st = sync_manager.state
    gp = st.get("gate_page")  # 必须在 start() 之前取：start 会把 gate_page 重置为 None
    if st.get("needs_verify") and not st.get("running"):
        # 全量/续抓卡在验证码上时不能强制 update（该板块可能还没完成全量），
        # 交给 start() 按断点自动选 full / resume
        resume_mode = "update" if st.get("mode") == "update" else None
        try:
            sync_manager.start(st.get("section") or 1, mode=resume_mode, start_page=gp)
            return {"success": True,
                    "message": f"验证通过，已从第 {gp or 1} 页继续同步"}
        except (ValueError, RuntimeError) as exc:
            logger.info("验证后自动续抓未启动：%s", exc)

    sec = st.get("manual_gate_section")
    if sec and not st.get("running"):
        st["manual_gate_section"] = None
        try:
            sync_manager.start(int(sec), mode="update", start_page=1)
            return {"success": True,
                    "message": f"验证通过，已开始{SECTIONS.get(int(sec), '')}增量更新"}
        except ValueError:
            # 该板块还没完成全量，退回自动模式（有断点续抓 / 否则全量）
            try:
                sync_manager.start(int(sec))
                return {"success": True,
                        "message": f"验证通过，已开始{SECTIONS.get(int(sec), '')}同步"}
            except (ValueError, RuntimeError) as exc:
                return {"success": True, "message": f"验证通过，但自动同步未启动：{exc}"}
        except RuntimeError:
            return {"success": True, "message": "验证通过，同步已在进行中"}
    return {"success": True, "message": "验证通过，可以继续同步了"}


# ---- 企业微信：配置 / 测试 / 验证码推送 / 回调 ----

def _mask_secret(value: str) -> str:
    if not value:
        return ""
    return (value[:2] + "****" + value[-2:]) if len(value) > 8 else "****"


def _normalize_callback_url(raw: str) -> str:
    """校验并补全回调地址。

    企业微信「接收消息」只接受 https + 有效证书的地址；这里只做格式检查，
    是否真的公网可达要在企微后台保存时才知道。
    只填域名时自动补上 /wechat/callback。
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if not url.lower().startswith("https://"):
        raise HTTPException(status_code=400,
                            detail="回调地址必须以 https:// 开头（企业微信不接受 http）")
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="回调地址里缺少完整域名")
    path = parsed.path.rstrip("/")
    if not path:
        return url.rstrip("/") + "/wechat/callback"
    if path != "/wechat/callback":
        raise HTTPException(status_code=400,
                            detail="路径必须是 /wechat/callback（服务端只在这个路径上收消息）")
    return url.replace(path, "/wechat/callback")


@app.get("/api/wecom/config")
async def wecom_config_view() -> dict:
    """企业微信配置（secret 类字段只回掩码，不回明文）"""
    cfg = wecom.load_config()
    st = sync_manager.state
    return {
        "corpid": cfg["corpid"],
        "agentid": cfg["agentid"],
        "touser": cfg["touser"],
        "has_secret": bool(cfg["corpsecret"]),
        "secret_masked": _mask_secret(cfg["corpsecret"]),
        "has_token": bool(cfg["token"]),
        "token_masked": _mask_secret(cfg["token"]),
        "has_aeskey": bool(cfg["aeskey"]),
        "aeskey_masked": _mask_secret(cfg["aeskey"]),
        "callback_url": cfg["callback_url"],
        "configured": wecom.is_configured(cfg),
        "gate_pending": bool(st.get("gate_key")),
        "gate_page": st.get("gate_page"),
    }


@app.post("/api/wecom/config")
async def wecom_config_save(body: dict) -> dict:
    """保存企业微信配置。secret 类字段留空表示保持原值。"""
    body = dict(body)
    body["callback_url"] = _normalize_callback_url(body.get("callback_url") or "")
    cfg = wecom.save_config(body)
    logger.info("企业微信配置已更新（corpid=%s, agentid=%s）",
                _mask_secret(cfg["corpid"]), cfg["agentid"])
    return {"success": True, "message": "企业微信配置已保存",
            "configured": wecom.is_configured(cfg)}


@app.post("/api/wecom/test")
async def wecom_test() -> dict:
    """发送一条测试文本，验证配置是否可用"""
    ok, msg = await asyncio.to_thread(
        wecom.send_text, "【2bt0 资源库】企业微信推送测试成功。")
    return {"success": ok, "message": msg}


@app.post("/api/wecom/menu")
async def wecom_menu() -> dict:
    """安装默认自定义菜单（同步 / 状态 / 帮助）"""
    ok, msg = await asyncio.to_thread(wecom.create_menu)
    return {"success": ok, "message": msg}


@app.post("/api/wecom/gate")
async def wecom_push_gate() -> dict:
    """手动把当前图形验证码推送到企业微信（验证码过期时可重新推一张）"""
    if not wecom.is_configured():
        return {"success": False, "message": "请先配置企业微信"}
    try:
        cap = await asyncio.to_thread(gate_captcha)
    except Exception as exc:
        return {"success": False, "message": f"获取验证码失败: {exc}"}
    key = cap.get("key") or ""
    if not key:
        return {"success": False, "message": "站点未返回验证码"}
    st = sync_manager.state
    st["gate_key"] = key
    # 记住板块：手动推送的验证码通过后自动起一次增量更新（没有真实断点页）
    st["manual_gate_section"] = st.get("section") or 1
    ok, msg = await asyncio.to_thread(
        wecom.notify_captcha, SECTIONS.get(st.get("section"), ""),
        st.get("gate_page") or st.get("page") or 1, cap)
    return {"success": ok, "message": msg}


def _wecom_crypto(cfg: dict):
    """按配置构造回调加解密器；未配置完整时返回 (None, 错误文案)"""
    token, aeskey, corpid = cfg["token"], cfg["aeskey"], cfg["corpid"]
    if not token or not aeskey or not corpid:
        return None, "未配置企业微信 Token / EncodingAESKey / 企业 ID"
    try:
        return wecom.WeChatCrypto(token, aeskey, corpid), ""
    except ValueError as exc:
        return None, str(exc)


@app.get("/wechat/callback")
async def wecom_callback_verify(request: Request) -> PlainTextResponse:
    """企业微信后台「接收消息」URL 校验：验签后原样返回解密的 echostr"""
    crypto, err = _wecom_crypto(wecom.load_config())
    if not crypto:
        return PlainTextResponse(err, status_code=500)
    q = request.query_params
    signature = q.get("msg_signature") or q.get("signature") or ""
    timestamp, nonce, echostr = q.get("timestamp", ""), q.get("nonce", ""), q.get("echostr", "")
    if not signature or not timestamp or not nonce:
        return PlainTextResponse("success")
    if not crypto.verify_signature(signature, timestamp, nonce, echostr):
        logger.warning("企业微信回调 URL 校验：签名不匹配")
        return PlainTextResponse("签名验证失败", status_code=403)
    try:
        plain, _ = crypto.decrypt_message(echostr)
        return PlainTextResponse(plain)
    except Exception:
        logger.exception("企业微信回调 URL 校验：echostr 解密失败")
        return PlainTextResponse("解密失败", status_code=403)


@app.post("/wechat/callback")
async def wecom_callback_message(request: Request) -> PlainTextResponse:
    """接收企业微信消息：回复的数字即图形验证码，校验通过后自动续抓。

    校验要访问 2bt0，可能超过企微的 5 秒回调超时，所以先回「收到」，
    结果再用应用消息推回给发送人。
    """
    cfg = wecom.load_config()
    crypto, err = _wecom_crypto(cfg)
    if not crypto:
        return PlainTextResponse(err, status_code=500)

    q = request.query_params
    signature = q.get("msg_signature") or q.get("signature") or ""
    timestamp, nonce = q.get("timestamp", ""), q.get("nonce", "")
    raw = await request.body()
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
        encrypted = root.findtext("Encrypt") or ""
    except Exception:
        logger.warning("企业微信回调：XML 解析失败")
        return PlainTextResponse("success")
    if not signature or not crypto.verify_signature(signature, timestamp, nonce, encrypted):
        logger.warning("企业微信回调：签名不匹配")
        return PlainTextResponse("签名验证失败", status_code=403)
    try:
        plain, from_user = crypto.decrypt_message(encrypted) if encrypted else \
            (raw.decode("utf-8", "replace"), "")
    except Exception:
        logger.exception("企业微信回调：消息解密失败")
        return PlainTextResponse("success")

    msg = wecom.parse_message(plain) or {}
    user = msg.get("FromUserName") or from_user
    corp = msg.get("ToUserName") or cfg["corpid"]
    content = (msg.get("Content") or "").strip()

    search_reply = (_handle_search_text(content, user)
                    if msg.get("MsgType") == "text" else None)
    if search_reply:
        reply = search_reply
    elif msg.get("MsgType") == "event" and msg.get("Event") == "click":
        key = (msg.get("EventKey") or "").strip()
        threading.Thread(target=_handle_menu_event, args=(key, user),
                         name="wecom-menu", daemon=True).start()
        reply = "收到，正在处理…"
    elif msg.get("MsgType") == "text" and re.fullmatch(r"[A-Za-z0-9]{4,6}", content):
        threading.Thread(target=_handle_gate_code, args=(content, user),
                         name="wecom-gate-code", daemon=True).start()
        reply = f"收到验证码 {content}，正在校验…"
    else:
        reply = ("直接回复图中验证码字符（字母/数字，4-6 位）；\n"
                 "要搜片请发：搜 片名")
    return PlainTextResponse(
        wecom.build_reply_xml(user, corp, reply, crypto),
        media_type="application/xml")


def _handle_gate_code(code: str, to_user: str) -> None:
    """后台校验企业微信回复的验证码：成功续抓，失败换一张重新推送。"""
    st = sync_manager.state
    key = st.get("gate_key") or ""
    if not key:
        wecom.send_text("当前没有等待验证的图形验证码。可在网页「同步」页手动获取，"
                        "或等下次同步撞上验证码时自动推送。", to_user)
        return
    try:
        verify_gate(key, code)
    except Exception as exc:
        logger.warning("企业微信回复的验证码 %s 未通过：%s", code, exc)
        wecom.send_text(f"验证码 {code} 未通过：{exc}\n已换一张，请回复新图中的字符。",
                        to_user)
        try:
            cap = gate_captcha()
            st["gate_key"] = cap.get("key") or ""
            if cap.get("key"):
                wecom.notify_captcha(SECTIONS.get(st.get("section"), ""),
                                     st.get("gate_page") or st.get("page") or 1,
                                     cap, to_user=to_user)
        except Exception:
            logger.exception("重新推送验证码失败")
        return

    logger.info("企业微信回复的验证码已通过（发送人 %s）", to_user or "-")
    st["gate_key"] = ""
    result = _resume_after_gate()
    wecom.send_text(f"2bt0 验证通过：{result['message']}", to_user)


_MENU_HELP = (
    "【2bt0 使用说明】\n"
    "· 搜片：直接发「搜 片名」，如：搜 沙丘（回复数字翻页，0 结束）\n"
    "· 菜单「同步」：电影/电视剧增量更新、停止同步\n"
    "· 菜单「状态」：同步进度、库存统计、重新推送验证码\n"
    "· 同步撞到图形验证码时自动推送 captcha.gif，直接回复图中字符即可\n"
    "· 验证通过后会自动从断点继续同步"
)


def _status_text() -> str:
    s = sync_manager.status()
    mode = {"full": "全量抓取", "resume": "断点续抓",
            "update": "增量更新"}.get(s.get("mode") or "", s.get("mode") or "-")
    lines = [
        "【2bt0 同步状态】",
        (f"运行中：{s.get('section_label') or '-'} 第 {s.get('page')} 页（{mode}）"
         if s.get("running") else "未运行"),
        f"库总量：{s.get('db_total', 0)} 条",
    ]
    progress = s.get("progress") or {}
    if progress:
        bits = ", ".join(f"{SECTIONS.get(int(k), k)} {v} 页"
                         for k, v in progress.items())
        lines.append(f"断点：{bits}")
    if s.get("message"):
        lines.append(f"最近：{s['message']}")
    return "\n".join(lines)


def _stats_text() -> str:
    s = sync_manager.status()
    cats = "、".join(f"{k} {v}" for k, v in (s.get("by_category") or {}).items()) or "-"
    return (f"【2bt0 库存】\n总量：{s.get('db_total', 0)} 条\n分类：{cats}\n"
            f"数据库：{s.get('db_size_mb', 0)} MB\n"
            f"最近入库：{s.get('last_seen') or '-'}")


def _start_update(section: int, to_user: str) -> None:
    label = SECTIONS.get(section, "")
    if sync_manager.state.get("running"):
        wecom.send_text("同步已在进行中，可点菜单「同步 → 停止同步」。", to_user)
        return
    if section not in get_sync_done("bt0"):
        wecom.send_text(f"{label}尚未完成全量同步，请先在网页「同步」页执行全量。",
                        to_user)
        return
    try:
        sync_manager.start(section, mode="update")
    except RuntimeError:
        wecom.send_text("同步已在进行中。", to_user)
        return
    wecom.send_text(f"已启动{label}增量更新，完成后会推送结果。", to_user)


SEARCH_PAGE_SIZE = 8  # 企微文本消息上限 2048 字节；当前磁力约 60 字，一页 8 条约 1KB
_SEARCH_SESSIONS: dict = {}  # {userid: {keyword, total, page, ts}}
_SEARCH_TTL = 600  # 翻页会话有效期（秒）


def _search_reply(keyword: str, to_user: str, page: int = 1) -> None:
    """搜索本地库并发送指定页（每页 4 条，带完整磁力链接）。"""
    page = max(1, page)
    try:
        rows, total = search_items(keyword, SEARCH_PAGE_SIZE,
                                   (page - 1) * SEARCH_PAGE_SIZE)
        if total == 0:
            # 片名没命中时再走 FTS（演职人员 / 别名等）
            rows, total = query_items(page=page, keyword=keyword,
                                      page_size=SEARCH_PAGE_SIZE)
    except Exception as exc:
        logger.exception("本地库搜索失败：%s", keyword)
        wecom.send_text(f"搜索失败：{exc}", to_user)
        return
    if not rows:
        if page > 1:
            wecom.send_text(f"「{keyword}」没有更多结果了。", to_user)
        else:
            wecom.send_text(f"本地库没有找到「{keyword}」。\n"
                            f"可能尚未同步到，可稍后再试或换关键词。", to_user)
        return
    pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE))
    _SEARCH_SESSIONS[to_user] = {"keyword": keyword, "total": total,
                                 "page": page, "ts": time.time()}
    lines = [f"【{keyword}】第 {page}/{pages} 页｜共 {total} 条"]
    start = (page - 1) * SEARCH_PAGE_SIZE
    for i, r in enumerate(rows, start + 1):
        name = r["movie_title"] or r["title"] or "（无标题）"
        lines.append(f"{i}. {name}｜{r['category'] or ''}｜{r['size'] or ''}")
        lines.append(r["magnet"] or f"magnet:?xt=urn:btih:{r['info_hash']}")
    if pages > 1:
        lines.append(f"回复页码翻页（1-{pages}），0 结束；换片发：搜 片名")
    wecom.send_text(wecom.truncate_reply("\n".join(lines)), to_user)


def _handle_search_text(content: str, to_user: str) -> str | None:
    """处理搜索指令与翻页数字；返回直接回复文本，None 表示不是搜索消息。"""
    m = re.match(r"^(?:搜索|搜|找)\s*[:：]?\s*(.+)$", content)
    if m:
        keyword = m.group(1).strip()
        if not keyword:
            return "请发送：搜 片名"
        threading.Thread(target=_search_reply, args=(keyword, to_user, 1),
                         name="wecom-search", daemon=True).start()
        return f"正在搜索「{keyword}」…"

    if not re.fullmatch(r"\d", content):
        return None
    sess = _SEARCH_SESSIONS.get(to_user)
    if not sess:
        return None
    if time.time() - sess["ts"] > _SEARCH_TTL:
        _SEARCH_SESSIONS.pop(to_user, None)
        return None
    if content == "0":
        _SEARCH_SESSIONS.pop(to_user, None)
        return "已结束搜索。"
    page = int(content)
    pages = max(1, math.ceil(sess["total"] / SEARCH_PAGE_SIZE))
    if page < 1 or page > pages:
        return f"页码超出范围：共 {pages} 页，回复 1-{pages} 或 0 结束。"
    threading.Thread(target=_search_reply,
                     args=(sess["keyword"], to_user, page),
                     name="wecom-search", daemon=True).start()
    return f"正在发送第 {page} 页…"


def _handle_menu_event(key: str, to_user: str) -> None:
    """处理企微自定义菜单点击（type=click 的 EventKey）"""
    st = sync_manager.state
    try:
        if key == "sync_update_1":
            _start_update(1, to_user)
        elif key == "sync_update_2":
            _start_update(2, to_user)
        elif key == "sync_stop":
            sync_manager.stop()
            wecom.send_text("已发送停止指令，当前页处理完后停止（断点已保存）。", to_user)
        elif key == "sync_status":
            wecom.send_text(_status_text(), to_user)
        elif key == "stats":
            wecom.send_text(_stats_text(), to_user)
        elif key == "captcha":
            cap = gate_captcha()
            if not (cap.get("key") or ""):
                wecom.send_text("站点未返回验证码，请稍后再试。", to_user)
                return
            st["gate_key"] = cap["key"]
            st["manual_gate_section"] = st.get("section") or 1
            ok, msg = wecom.notify_captcha(
                SECTIONS.get(st.get("section"), ""),
                st.get("gate_page") or st.get("page") or 1,
                cap, to_user=to_user)
            if not ok:
                wecom.send_text(f"验证码推送失败：{msg}", to_user)
        elif key == "search_hint":
            wecom.send_text("直接发送：搜 片名\n例如：搜 沙丘\n"
                            "每页 4 条，回复数字翻页，0 结束", to_user)
        else:
            wecom.send_text(_MENU_HELP, to_user)
    except Exception as exc:
        logger.exception("企业微信菜单指令处理失败：%s", key)
        wecom.send_text(f"处理失败：{exc}", to_user)


@app.get("/api/logs")
async def logs(lines: int = Query(200, ge=1, le=1000)) -> dict:
    """读取应用日志尾部（含同步进度）"""
    log_file = DATA_DIR / "logs" / "app.log"
    if not log_file.exists():
        return {"lines": []}
    # 只读文件尾部 512KB，避免日志增长后整读
    with open(log_file, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 512 * 1024))
        text = f.read().decode("utf-8", errors="replace")
    return {"lines": text.splitlines()[-lines:]}


# ---- 115 网盘转存 ----

@app.get("/api/115/config")
async def cloud115_config() -> dict:
    """115 配置：Cookie 是否已配置（掩码展示）、默认保存目录"""
    config = cloud115.load_config()
    cookie = config.get("cookie", "")
    masked = ""
    if cookie:
        # 加密后的密文也只取首尾各 4 位，不暴露明文也不暴露完整密文
        masked = (cookie[:4] + "****" + cookie[-4:]) if len(cookie) > 16 else "****"
    path_id, path_name = cloud115.get_default_save_path()
    return {"has_cookie": bool(cookie), "cookie_masked": masked,
            "save_path_id": path_id, "save_path_name": path_name}


@app.post("/api/115/config")
async def cloud115_set_config(body: dict) -> dict:
    """保存 115 Cookie（AES 加密存储）"""
    cookie = (body.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    cloud115.update_config(lambda c: c.__setitem__("cookie", encrypt(cookie)))
    cloud115.invalidate_cookie_cache()
    logger.info("115 Cookie 已更新")
    return {"success": True, "message": "Cookie 保存成功"}


@app.post("/api/115/verify")
async def cloud115_verify() -> dict:
    """验证当前 Cookie 是否有效"""
    success, msg = await asyncio.to_thread(cloud115.verify_cookie)
    return {"success": success, "message": msg}


@app.get("/api/115/dirs")
async def cloud115_dirs(cid: str = Query("0", max_length=30)) -> dict:
    """列出 115 网盘某目录下的子目录（用于选择转存目标目录）"""
    success, msg, dirs = await asyncio.to_thread(cloud115.get_dir_list, cid)
    return {"success": success, "message": msg, "dirs": dirs}


@app.get("/api/115/save_path")
async def cloud115_save_path() -> dict:
    path_id, path_name = cloud115.get_default_save_path()
    return {"path_id": path_id, "path_name": path_name}


@app.post("/api/115/save_path")
async def cloud115_set_save_path(body: dict) -> dict:
    path_id = (body.get("path_id") or "0").strip() or "0"
    path_name = (body.get("path_name") or "").strip() or None
    cloud115.set_default_save_path(path_id, path_name)
    logger.info("115 默认保存目录已更新：%s (%s)", path_name, path_id)
    return {"success": True, "message": "默认保存目录已更新"}


def _magnets_by_movie(movie_id: str) -> list[dict]:
    """取某部影片的全部版本磁力，返回 [{info_hash, magnet, title}]（115 转存用）"""
    rows, _ = magnets_for_movie(movie_id, limit=10000)
    return [{"info_hash": r["info_hash"], "magnet": r["magnet"], "title": r["title"]}
            for r in rows if r["magnet"]]


@app.post("/api/115/transfer")
async def cloud115_transfer(body: dict) -> dict:
    """转存磁力到 115。body 可传：
    - magnet: 直接转存该磁力链接
    - info_hash: 从本地库按 info_hash 查磁力
    - movie_id: 转存该片全部版本
    """
    magnet = (body.get("magnet") or "").strip()
    info_hash = (body.get("info_hash") or "").strip()
    movie_id = (body.get("movie_id") or "").strip()
    if not magnet and not info_hash and not movie_id:
        raise HTTPException(status_code=400, detail="需指定 magnet / info_hash / movie_id")

    save_path_id, save_path_name = cloud115.get_default_save_path()
    items = []  # [{info_hash, magnet, title}]
    if magnet:
        items.append({"info_hash": info_hash or "", "magnet": magnet, "title": body.get("title", "")})
    elif info_hash:
        rows, _ = query_items(keyword=info_hash, page_size=10)
        for r in rows:
            if r["info_hash"] == info_hash and r["magnet"]:
                items.append({"info_hash": info_hash, "magnet": r["magnet"],
                              "title": r["movie_title"] or r["title"]})
                break
    else:
        items = _magnets_by_movie(movie_id)

    if not items:
        raise HTTPException(status_code=400, detail="未找到对应的磁力链接")

    results = []
    for m in items:
        success, msg = await asyncio.to_thread(
            cloud115.add_offline_task, m["magnet"], save_path_id)
        results.append({"info_hash": m["info_hash"], "success": success, "message": msg})
        if success:
            await asyncio.sleep(1)
        try:
            transfer_history.add_record(
                info_hash=m["info_hash"], movie_title=m["title"], magnet=m["magnet"],
                success=success, message=msg, source="single")
        except Exception:
            logger.exception("转存历史记录失败")
    success_count = sum(1 for r in results if r["success"])
    return {"success": success_count == len(items),
            "message": f"成功 {success_count}/{len(items)}（保存到: {save_path_name}）",
            "results": results}


@app.get("/api/115/tasks")
async def cloud115_tasks(page: int = Query(1, ge=1)) -> dict:
    success, msg, tasks = await asyncio.to_thread(cloud115.get_task_list, page)
    return {"success": success, "message": msg, "tasks": tasks}


@app.get("/api/115/history/recent")
async def cloud115_history_recent(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"records": transfer_history.get_recent(limit)}


@app.get("/api/115/history/statistics")
async def cloud115_history_statistics() -> dict:
    return transfer_history.get_statistics()


def _static_version() -> str:
    """静态资源的缓存戳（取 app.js / style.css 的 mtime）。

    Cloudflare 会把 .js/.css 按扩展名缓存 4 小时（max-age=14400），origin 发
    no-cache 也管不住浏览器里已经存下的旧文件——表现就是"页面有新按钮，点了
    没反应"。给 URL 带上 mtime，改过才换地址，没改就继续用缓存。
    """
    stamp = 0
    for name in ("app.js", "style.css"):
        try:
            stamp = max(stamp, int((STATIC_DIR / name).stat().st_mtime))
        except OSError:
            pass
    return str(stamp)


@app.get("/")
async def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    ver = _static_version()
    html = html.replace("/static/app.js", f"/static/app.js?v={ver}")
    html = html.replace("/static/style.css", f"/static/style.css?v={ver}")
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
