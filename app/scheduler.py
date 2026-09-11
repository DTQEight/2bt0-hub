"""每日定时增量更新：每天固定时刻依次对电影、电视剧跑一次增量更新。

- 只做增量更新（扫前 10 页追新），不做全量；板块必须已完成全量同步
- 两个板块共用一个同步器，必须串行：电影跑完再跑电视剧
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

from db import get_setting, set_setting
from sources.bt0 import SECTIONS
from sync import sync_manager

logger = logging.getLogger("resource-hub.scheduler")

# 默认触发小时（0-23 点，容器本地时间）；网页上改过之后以数据库为准
DEFAULT_HOUR = int(os.getenv("SYNC_HOUR", "3"))
# 调度线程轮询间隔：兼作"重读配置"和"等待到点"的粒度
POLL_SECONDS = 30

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

def run_daily_incremental() -> None:
    """依次对电影、电视剧执行增量更新（一个跑完再跑下一个）"""
    logger.info("每日增量更新开始")
    for section in sorted(SECTIONS):
        label = SECTIONS[section]
        if sync_manager.state["running"]:
            logger.warning("每日增量：已有同步在进行，跳过%s", label)
            continue
        try:
            sync_manager.start(section, mode="update")
        except (ValueError, RuntimeError) as exc:
            logger.warning("每日增量：%s 跳过（%s）", label, exc)
            continue
        while sync_manager.state["running"]:
            time.sleep(2)
        logger.info("每日增量：%s 结束（%s）", label, sync_manager.state["message"])
    logger.info("每日增量更新结束")


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
