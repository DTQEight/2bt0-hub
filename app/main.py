"""Web 服务：2bt0 磁力资源库 API + 前端页面 + 后台全量同步。"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import math
import os
import re
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from api_auth import (api_key_for_admin, get_api_key, is_public_api_path,
                      require_api_key)
from db import (filter_options, get_movie, get_sync_progress, init_db,
                magnet_by_hash, magnets_for_movie, query_groups, query_items,
                quality_counts, upsert_items)
import quality
from movies import movie_detail_manager
from posters import migrate_to_shards, resolve as resolve_poster
from scheduler import get_schedule, next_run_at, set_schedule, start_scheduler
from sources import SourceError, get_source
from sources.bt0 import SECTIONS
from sync import sync_manager
import cloud115
import transfer_history
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

init_db()  # 本地磁力库建表
# 对外 API 密钥：首次启动生成并打印一次到日志（之后从库里读，不重复打印）
get_api_key()


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
    """全站 API 密钥校验：所有 /api/* 都要带 X-API-Key，只有这几类豁免——
    /api/health（容器 healthcheck 依赖）、/api/apikey（查看密钥本身）、
    /api/docs 与 /api/openapi.json（文档）。

    用中间件而不是逐个路由加依赖：新增接口自动受保护，不会有人漏加。
    网页自己不豁免，而是由 app.js 自动带上密钥（见 static/app.js 顶部）。
    """
    path = request.url.path
    if path.startswith("/api/") and not is_public_api_path(path):
        try:
            require_api_key(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return await call_next(request)


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
    """修改定时任务。body: {enabled?: bool, hour?: 0-23}"""
    try:
        cfg = set_schedule(enabled=body.get("enabled"), hour=body.get("hour"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"参数无效：{exc}") from exc
    logger.info("定时任务配置已更新：%s，每天 %02d:00",
                "已开启" if cfg["enabled"] else "已关闭", cfg["hour"])
    return _schedule_payload(cfg)


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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
