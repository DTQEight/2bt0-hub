"""Web 服务：2bt0 磁力资源库 API + 前端页面 + 后台全量同步。"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import math
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import (filter_options, get_movie, get_sync_progress, init_db, query_groups,
                upsert_items)
from movies import movie_detail_manager
from scheduler import get_schedule, next_run_at, set_schedule, start_scheduler
from sources import SourceError, get_source
from sources.bt0 import SECTIONS
from sync import sync_manager

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

# 容器启动时若存在未完成的同步断点，自动续抓（重建容器也不丢进度）
_pending = get_sync_progress("bt0")
if _pending:
    _sec = min(_pending)
    try:
        sync_manager.start(_sec, start_page=_pending[_sec] + 1, mode="resume")
        logger.info("检测到未完成断点，自动续抓：%s（板块 %d）从第 %d 页",
                    SECTIONS[_sec], _sec, _pending[_sec] + 1)
    except Exception:
        logger.exception("自动续抓启动失败，可稍后手动点击同步按钮")

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
) -> dict:
    name = source
    try:
        result = await get_source(name).fetch_page(
            page=page, query=q.strip(), section=sc, category=category.strip(),
            movie_id=movie_id.strip())
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
    """海报优先用本地缓存（/posters/xxx.webp），没缓存或文件已丢则回落到图床原地址"""
    name = str(rec.pop("poster_path", "") or "")
    if name and (POSTER_DIR / name).is_file():
        rec["image"] = f"/posters/{name}"
    return rec


@app.get("/posters/{name}")
async def poster(name: str) -> FileResponse:
    """本地缓存的海报图（拉影片详情时从图床下载，见 posters.py）"""
    if not _POSTER_RE.match(name):
        raise HTTPException(status_code=404, detail="海报不存在")
    path = POSTER_DIR / name
    if not path.is_file():
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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
