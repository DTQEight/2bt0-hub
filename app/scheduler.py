"""每日定时增量更新：每天固定时刻依次对电影、电视剧跑一次增量更新。

- 只做增量更新（扫前 10 页追新），不做全量；板块必须已完成全量同步
- 两个板块共用一个同步器，必须串行：电影跑完再跑电视剧
- 增量结束后自动拉取当天新增影片的详情（海报/评分等），最后备份数据库
- 已有同步在跑（例如手动全量）时跳过当天，避免互相打断
- 开关与触发小时存在数据库里，网页「同步」页可随时修改，调度线程每 30 秒读取一次
- 容器时区由 TZ 决定（compose 里为 Asia/Shanghai），按容器本地时间触发
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from db import backup_to, get_setting, set_setting
from movies import movie_detail_manager
from sources.bt0 import SECTIONS
from sync import sync_manager

logger = logging.getLogger("resource-hub.scheduler")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
# 默认触发小时（0-23 点，容器本地时间）；网页上改过之后以数据库为准
DEFAULT_HOUR = int(os.getenv("SYNC_HOUR", "3"))
# 调度线程轮询间隔：兼作"重读配置"和"等待到点"的粒度
POLL_SECONDS = 30
# 每日增量跑完后自动备份，只保留最近 N 份
BACKUP_KEEP = 7

_KEY_ENABLED = "schedule.enabled"
_KEY_HOUR = "schedule.hour"


# ---- 配置读写（供网页 UI 调用） ----

def get_schedule() -> dict:
    """读取定时任务配置：{"enabled": bool, "hour": int}"""
    try:
        hour = int(get_setting(_KEY_HOUR, str(DEFAULT_HOUR)))
    except ValueError:
        hour = DEFAULT_HOUR
    if not 0 <= hour <= 23:
        hour = DEFAULT_HOUR
    return {"enabled": get_setting(_KEY_ENABLED, "1") == "1", "hour": hour}


def set_schedule(enabled: bool | None = None, hour: int | None = None) -> dict:
    """修改定时任务配置（只改传入的字段），返回修改后的配置"""
    if enabled is not None:
        set_setting(_KEY_ENABLED, "1" if enabled else "0")
    if hour is not None:
        h = int(hour)  # 非数字会抛 ValueError，由调用方转成 400
        if not 0 <= h <= 23:
            raise ValueError("hour 必须在 0-23 之间")
        set_setting(_KEY_HOUR, str(h))
    return get_schedule()


def next_run_at(cfg: dict | None = None) -> datetime | None:
    """下次执行时刻；已关闭时返回 None"""
    cfg = cfg or get_schedule()
    if not cfg["enabled"]:
        return None
    now = datetime.now()
    target = now.replace(hour=cfg["hour"], minute=0, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


# ---- 执行 ----

def backup_database() -> None:
    """备份数据库：VACUUM INTO 生成带日期的紧凑快照，只保留最近 BACKUP_KEEP 份"""
    backup_dir = DATA_DIR / "backups"
    target = backup_dir / f"magnets-{datetime.now():%Y%m%d}.db"
    try:
        backup_to(target)
    except Exception:
        logger.exception("数据库备份失败（不影响同步）")
        return
    logger.info("数据库已备份：%s（%.1f MB）", target.name,
                target.stat().st_size / 1048576)
    for old in sorted(backup_dir.glob("magnets-*.db"))[:-BACKUP_KEEP]:
        try:
            old.unlink()
            logger.info("已清理旧备份：%s", old.name)
        except OSError:
            logger.warning("旧备份清理失败：%s", old.name)


def fetch_new_movie_details() -> None:
    """拉取新增影片的详情（海报/年份/评分），供本地库海报墙显示。

    只处理待拉取队列（种子里出现过、movies 表还没有的影片），
    所以已有详情的老片不会重复请求；队列为空时直接跳过。
    """
    if sync_manager.state["running"]:
        logger.warning("影片详情：同步仍在进行，跳过本次")
        return
    if movie_detail_manager.state["running"]:
        logger.info("影片详情：已有任务在进行，跳过本次")
        return
    try:
        movie_detail_manager.start()
    except (ValueError, RuntimeError) as exc:
        logger.info("影片详情：无需拉取（%s）", exc)
        return
    logger.info("影片详情：开始拉取新增影片（共 %d 部）",
                movie_detail_manager.state["total"])
    while movie_detail_manager.state["running"]:
        time.sleep(2)
    logger.info("影片详情：结束（%s）", movie_detail_manager.state["message"])


def run_daily_incremental() -> None:
    """依次对电影、电视剧执行增量更新（一个跑完再跑下一个），
    随后拉取新增影片详情，最后备份数据库"""
    logger.info("每日增量更新开始")
    for section in sorted(SECTIONS):
        label = SECTIONS[section]
        if sync_manager.state["running"]:
            logger.warning("每日增量：已有同步在进行，跳过%s", label)
            continue
        try:
            # auto_details=False：两个板块都跑完后由本函数统一拉详情，避免每跑完
            # 一个板块就触发一次（也与下一板块的同步叠加请求）
            sync_manager.start(section, mode="update", auto_details=False)
        except (ValueError, RuntimeError) as exc:
            logger.warning("每日增量：%s 跳过（%s）", label, exc)
            continue
        while sync_manager.state["running"]:
            time.sleep(2)
        logger.info("每日增量：%s 结束（%s）", label, sync_manager.state["message"])
    logger.info("每日增量更新结束")
    fetch_new_movie_details()
    backup_database()


def _loop() -> None:
    pending: datetime | None = None
    hour: int | None = None
    while True:
        cfg = get_schedule()
        changed = cfg["hour"] != hour
        hour = cfg["hour"]
        if not cfg["enabled"]:
            if pending is not None:
                logger.info("每日增量更新已关闭，不再排期")
            pending = None
        elif pending is None or changed:
            pending = next_run_at(cfg)
            logger.info("每日增量更新已排期：%s（每天 %02d:00）",
                        pending.strftime("%Y-%m-%d %H:%M"), hour)
        elif datetime.now() >= pending:
            run_daily_incremental()
            pending = next_run_at(cfg)
            logger.info("每日增量更新下次排期：%s", pending.strftime("%Y-%m-%d %H:%M"))
        time.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    """启动后台调度线程（daemon，随进程退出）"""
    threading.Thread(target=_loop, name="daily-scheduler", daemon=True).start()
