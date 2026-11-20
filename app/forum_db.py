"""115transfer 论坛磁力库 → 本地磁力索引。

2bt0 站点 2026-11 中旬起对未登录请求拒绝返回磁力（「请登录并开通VIP」），
列表里的 zlink 也只剩空占位。同一批资源在 115transfer 的论坛监控库
（forum_monitor.db）里有磁力，帖子标题 = 2bt0 发布名 + 空格 + 体积，
按标题前缀即可对上（方括号里的「+」论坛写作「/」，归一化后约 88% 命中）。

115transfer 的数据目录以只读方式挂到本容器 /forum（见 compose），
本模块把它全量导入 magnets.db 的 forum_magnets 表；同步时只查本地表，
不依赖网络、不消耗站点额度。论坛每天 04:00 增量采集，每日定时任务里
刷新一次，容器启动时也导入一次（保证断点续抓能直接用上）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from db import forum_norm, forum_stats, forum_upsert, get_setting, set_setting

logger = logging.getLogger("resource-hub.forum")

# 115transfer 论坛监控库（compose 里把其 data 目录只读挂到 /forum）
FORUM_DB = Path(os.getenv("FORUM_DB", "/forum/forum_monitor.db"))
# 同一进程内两次导入的最小间隔（秒）：页面/接口反复触发时不重复扫 27 万帖
MIN_INTERVAL = 1800
BATCH = 5000  # 每批写入本地库的磁力条数（控制内存峰值）

_import_lock = threading.Lock()
_missing_logged = False

# 论坛标题末尾的体积，如 "... 27.39GB"
_SIZE_RE = re.compile(r"\s(\d+(?:\.\d+)?\s?(?:TB|GB|MB))\s*$", re.I)


def available() -> bool:
    """论坛库是否可见（没挂载时返回 False，不影响主服务启动）"""
    try:
        return FORUM_DB.is_file()
    except OSError:
        return False


def _connect() -> sqlite3.Connection | None:
    """只读打开论坛库。

    115transfer 用 WAL 模式；只读挂载时 SQLite 需要写 -shm，若直接打开失败，
    退回复制一份（含 -wal/-shm）到 /data/tmp 再读。
    """
    if not available():
        global _missing_logged
        if not _missing_logged:
            logger.info("论坛磁力库不存在（%s），电影磁力将回退站点接口", FORUM_DB)
            _missing_logged = True
        return None
    try:
        conn = sqlite3.connect(f"file:{FORUM_DB}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn
    except sqlite3.Error as exc:
        logger.info("论坛库只读打开失败（%s），改为复制一份再读", exc)
    try:
        tmp = Path(os.getenv("DATA_DIR", "/data")).resolve() / "tmp" / "forum_monitor.db"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FORUM_DB, tmp)
        for suffix in ("-wal", "-shm"):
            src = Path(str(FORUM_DB) + suffix)
            if src.is_file():
                shutil.copyfile(src, str(tmp) + suffix)
        conn = sqlite3.connect(str(tmp), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM threads LIMIT 1")
        return conn
    except (OSError, sqlite3.Error) as exc:
        logger.warning("论坛库复制后仍无法读取：%s", exc)
        return None


def _rows(conn: sqlite3.Connection):
    """逐条产出 (tid, magnet, title, norm, size, post_date, forum_name, fetched_at)"""
    cur = conn.execute(
        "SELECT tid, title, forum_name, post_date, fetched_at, magnet_links"
        "  FROM threads"
        " WHERE magnet_links IS NOT NULL AND magnet_links NOT IN ('', '[]')")
    for r in cur:
        title = str(r["title"] or "").strip()
        if not title:
            continue
        try:
            links = json.loads(r["magnet_links"] or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(links, list):
            continue
        try:
            tid = int(r["tid"])
        except (TypeError, ValueError):
            continue
        norm = forum_norm(title)
        m = _SIZE_RE.search(title)
        size = m.group(1) if m else ""
        for link in links:
            magnet = str((link or {}).get("magnet") or "").strip()
            if magnet.startswith("magnet:"):
                yield (tid, magnet, title, norm, size, str(r["post_date"] or ""),
                       str(r["forum_name"] or ""), str(r["fetched_at"] or ""))


def import_magnets() -> dict:
    """把论坛库全量导入 forum_magnets，返回统计（失败返回 {}）。"""
    conn = _connect()
    if conn is None:
        return {}
    t0 = time.monotonic()
    total = 0
    batch: list[tuple] = []
    max_fetched = ""
    try:
        for row in _rows(conn):
            batch.append(row)
            if row[7] > max_fetched:
                max_fetched = row[7]
            if len(batch) >= BATCH:
                total += forum_upsert(batch)
                batch.clear()
        if batch:
            total += forum_upsert(batch)
    finally:
        conn.close()
    elapsed = round(time.monotonic() - t0, 1)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    set_setting("forum.imported_at", now)
    set_setting("forum.source_updated_at", max_fetched)
    logger.info("论坛磁力导入完成：%d 条（源库更新至 %s），耗时 %.1fs",
                total, max_fetched or "?", elapsed)
    return {"magnets": total, "seconds": elapsed,
            "source_updated_at": max_fetched, "imported_at": now}


def ensure_imported(force: bool = False) -> dict:
    """按需导入：距上次导入不足 MIN_INTERVAL 秒则跳过（force=True 强制）。"""
    with _import_lock:
        if not force:
            last = get_setting("forum.imported_at", "")
            if last:
                try:
                    age = (datetime.now()
                           - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")).total_seconds()
                except ValueError:
                    age = MIN_INTERVAL + 1
                if 0 <= age < MIN_INTERVAL:
                    return {"skipped": True, **forum_stats()}
        try:
            return import_magnets()
        except Exception:
            logger.exception("论坛磁力导入异常（继续使用已有数据）")
            return {}
