"""Web 服务：2bt0 磁力资源库 API + 前端页面 + 后台全量同步。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import count_all, get_sync_progress, init_db, upsert_items
from scheduler import get_schedule, next_run_at, set_schedule, start_scheduler
from sources import SourceError, get_source, list_sources
from sources.bt0 import SECTIONS
from sync import sync_manager

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_SOURCE = os.getenv("SOURCE", "bt0")

for sub in ("logs", "db", "tmp"):
    (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(DATA_DIR / "logs" / "app.log", encoding="utf-8"),
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


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "data_dir": str(DATA_DIR), "default_source": DEFAULT_SOURCE}


@app.get("/api/sources")
async def sources() -> dict:
    return {"sources": list_sources(), "default": DEFAULT_SOURCE}


@app.get("/api/items")
async def items(
    page: int = Query(1, ge=1),
    q: str = Query("", max_length=200),
    source: str = Query("bt0", max_length=50),
    sc: int = Query(1, ge=1, le=2, description="板块：1=电影 2=电视剧"),
) -> dict:
    name = source or DEFAULT_SOURCE
    try:
        result = await get_source(name).fetch_page(page=page, query=q.strip(), section=sc)
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


# ---- 本地库统计与 2bt0 全量同步 ----

@app.get("/api/db/stats")
async def db_stats() -> dict:
    return {"total": count_all()}


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
