"""定时增量更新：每天在若干固定时刻依次对电影、电视剧跑一次增量更新。

- 只做增量更新（扫前 10 页追新），不做全量；板块必须已完成全量同步
- 两个板块共用一个同步器，必须串行：电影跑完再跑电视剧
- 每个板块跑完自动跟进该板块的新增影片详情（海报/评分等）
- 不自动备份数据库（日常快照性价比太低，理由见 run_daily_incremental）
- 已有同步在跑（例如手动全量）时跳过当次，避免互相打断
- 开关与触发时刻（可多个，如 0/6/12/18 点）存在数据库里，网页「同步」页可随时修改，
  调度线程每 30 秒读取一次
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
from sources.bt0 import SECTIONS
from sync import sync_manager

logger = logging.getLogger("resource-hub.scheduler")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
# 默认触发时刻（0-23 点，容器本地时间，逗号分隔）；网页上改过之后以数据库为准。
# 站点每个板块只暴露最新 400 条，一天新增曾达 241 条，一天一跑有溢出窗口
# （多出的旧条目滑出列表就永久取不到了），故默认每 6 小时一次。
DEFAULT_HOURS = [int(h) for h in os.getenv("SYNC_HOURS", "0,6,12,18").split(",") if h.strip()]
# 调度线程轮询间隔：兼作"重读配置"和"等待到点"的粒度
POLL_SECONDS = 30
# 手动备份（backup_database）时保留最近 N 份；自动备份已关闭
BACKUP_KEEP = 7

_KEY_ENABLED = "schedule.enabled"
_KEY_HOURS = "schedule.hours"
_KEY_HOUR = "schedule.hour"  # 旧版单时刻配置，仅用于迁移


# ---- 配置读写（供网页 UI 调用） ----

def _parse_hours(raw: str) -> list[int]:
    """把 "0,6,12,18" 解析成去重升序的小时列表；有空值/越界时回退默认值"""
    hours: list[int] = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            h = int(part)
        except ValueError:
            return list(DEFAULT_HOURS)
        if not 0 <= h <= 23:
            return list(DEFAULT_HOURS)
        if h not in hours:
            hours.append(h)
    return sorted(hours) or list(DEFAULT_HOURS)


def get_schedule() -> dict:
    """读取定时任务配置：{"enabled": bool, "hours": [int, ...]}"""
    raw = get_setting(_KEY_HOURS, "")
    if not raw:
        legacy = get_setting(_KEY_HOUR, "").strip()  # 旧版只存了一个小时
        raw = legacy if legacy.isdigit() else ""
    return {"enabled": get_setting(_KEY_ENABLED, "1") == "1",
            "hours": _parse_hours(raw) if raw else list(DEFAULT_HOURS)}


def set_schedule(enabled: bool | None = None,
                 hours: list[int] | str | None = None) -> dict:
    """修改定时任务配置（只改传入的字段），返回修改后的配置"""
    if enabled is not None:
        set_setting(_KEY_ENABLED, "1" if enabled else "0")
    if hours is not None:
        vals: list[int] = []
        for v in (hours if isinstance(hours, (list, tuple)) else [hours]):
            h = int(v)  # 非数字会抛 ValueError，由调用方转成 400
            if not 0 <= h <= 23:
                raise ValueError("执行时刻必须在 0-23 之间")
            if h not in vals:
                vals.append(h)
        if not vals:
            raise ValueError("至少要保留一个执行时刻")
        set_setting(_KEY_HOURS, ",".join(str(h) for h in sorted(vals)))
    return get_schedule()


def next_run_at(cfg: dict | None = None) -> datetime | None:
    """下次执行时刻（所有配置时刻中最近的一个）；已关闭时返回 None"""
    cfg = cfg or get_schedule()
    if not cfg["enabled"]:
        return None
    now = datetime.now()
    for h in cfg["hours"]:
        target = datetime(now.year, now.month, now.day, h, 0, 0)
        if target > now:
            return target
    first = cfg["hours"][0]
    tomorrow = now + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, first, 0, 0)


# ---- 执行 ----

def backup_database() -> None:
    """手动备份数据库：VACUUM INTO 生成带日期的紧凑快照，只保留最近 BACKUP_KEEP 份。

    已不再由每日增量自动调用（见 run_daily_incremental 的说明），
    在改动数据结构、重抓全量之前手动跑一次即可：
        docker exec resource-hub python -c "import scheduler; scheduler.backup_database()"
    """
    backup_dir = DATA_DIR / "backups"
    target = backup_dir / f"magnets-{datetime.now():%Y%m%d}.db"
    # VACUUM INTO 目标已存在时会报错（同一天再次触发：手动跑增量/时区跳变）。
    # 先写临时名再原子替换，避免"删旧之后备份失败"两手空空
    tmp = target.with_name(target.name + ".tmp")
    try:
        backup_to(tmp)
        os.replace(tmp, target)
    except Exception:
        logger.exception("数据库备份失败（不影响同步）")
        tmp.unlink(missing_ok=True)
        return
    logger.info("数据库已备份：%s（%.1f MB）", target.name,
                target.stat().st_size / 1048576)
    # 只清理本函数生成的自动命名快照，别动手动命名的文件（如 manual-*.db）；
    # 单独 glob 而不混排，避免名字排序把不该删的挤出去
    for old in sorted(backup_dir.glob("magnets-*.db"))[:-BACKUP_KEEP]:
        try:
            old.unlink()
            logger.info("已清理旧备份：%s", old.name)
        except OSError:
            logger.warning("旧备份清理失败：%s", old.name)


def run_daily_incremental() -> None:
    """依次对电影、电视剧执行增量更新（一个跑完再跑下一个）。

    每个板块跑完后，同步器会自动跟进该板块的新增影片详情（海报/评分）。

    这里不再自动备份数据库：每天新增只有几十到几千条、约 1~2 MB，
    而一份 VACUUM 快照约 640 MB，日备等于拿 640 MB 存 1 MB 的变化，
    还会让备份目录按 640 MB/天 无上限增长。库里所有内容站点上都在、
    都能重抓，日常快照防的只是"重抓 3~8 小时"这点代价。需要还原点时
    手动调用 backup_database()（改动结构或全量重抓之前跑一次）。
    """
    logger.info("每日增量更新开始")
    # 先刷新论坛磁力库（115transfer 每天 04:00 采集完），电影磁力从这里匹配
    try:
        import forum_db
        info = forum_db.ensure_imported(force=True)
        if info.get("magnets"):
            logger.info("论坛磁力库已刷新：%d 条（源库更新至 %s）",
                        info["magnets"], info.get("source_updated_at") or "?")
    except Exception:
        logger.exception("论坛磁力库刷新失败（继续用已有数据）")
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
    hours_key: tuple[int, ...] | None = None
    while True:
        cfg = get_schedule()
        key = tuple(cfg["hours"])
        changed = key != hours_key
        hours_key = key
        if not cfg["enabled"]:
            if pending is not None:
                logger.info("定时增量更新已关闭，不再排期")
            pending = None
        elif pending is None or changed:
            pending = next_run_at(cfg)
            logger.info("定时增量更新已排期：%s（每天 %s）",
                        pending.strftime("%Y-%m-%d %H:%M"),
                        "、".join(f"{h:02d}:00" for h in cfg["hours"]))
        elif datetime.now() >= pending:
            run_daily_incremental()
            pending = next_run_at(cfg)
            logger.info("定时增量更新下次排期：%s", pending.strftime("%Y-%m-%d %H:%M"))
        time.sleep(POLL_SECONDS)


def start_scheduler() -> None:
    """启动后台调度线程（daemon，随进程退出）"""
    threading.Thread(target=_loop, name="daily-scheduler", daemon=True).start()
