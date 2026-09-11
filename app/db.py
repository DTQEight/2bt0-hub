"""磁力链接本地数据库（SQLite）。

存储位置：DATA_DIR/db/magnets.db（docker 部署时落在宿主机 data/db/ 目录）。

写入来源（全部按 info_hash 去重，重复时保留更完整的字段）：
- /api/items 浏览时：各数据源返回的磁力条目
- /api/magnet 按需解析：种子文件解析出的磁力
- 后台全量同步（sync.py）：2bt0 全部分页
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

_LOCK = threading.Lock()  # 序列化写事务


def _db_path() -> Path:
    data_dir = Path(os.getenv("DATA_DIR", "/data")).resolve()
    db_dir = data_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "magnets.db"


@contextmanager
def _db():
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")  # 允许读写并发
        with conn:  # 事务：异常时回滚
            yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS magnets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    info_hash TEXT NOT NULL UNIQUE,
    magnet TEXT NOT NULL,
    title TEXT DEFAULT '',
    size TEXT DEFAULT '',
    published_at TEXT DEFAULT '',
    category TEXT DEFAULT '',
    detail_url TEXT DEFAULT '',
    source TEXT DEFAULT '',
    torrent_name TEXT DEFAULT '',
    trackers TEXT DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_magnets_category ON magnets(category);
CREATE INDEX IF NOT EXISTS idx_magnets_last_seen ON magnets(last_seen_at);
-- 只保留真正被查询用到的两个索引：
--   category     → get_stats() 的 GROUP BY category
--   last_seen_at → get_stats() 的 MAX(last_seen_at)
-- 原先还有 title / source 两个索引，但现有查询用不上：关键词检索是
-- LIKE '%kw%'（前置通配符无法走 B-tree 索引），也没有按 source 过滤的语句。
-- 二者在 82 万行时占用约 93MB 并拖慢每行写入，故不再创建，并清理存量。
DROP INDEX IF EXISTS idx_magnets_title;
DROP INDEX IF EXISTS idx_magnets_source;
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    last_page INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def init_db() -> None:
    with _LOCK:
        with _db() as conn:
            conn.executescript(_SCHEMA)


_HASH_RE = re.compile(r"btih:([0-9a-fA-F]{40})")


def _hash_of(magnet: str) -> str:
    m = _HASH_RE.search(magnet or "")
    return m.group(1).lower() if m else ""


# 站点对 24 小时内的新资源返回相对时间（如 "9小时前"），入库时统一换算成绝对日期
_REL_RE = re.compile(r"^(\d+)\s*(分钟|小时|天)前?$")
_REL_FIXED = {"刚刚": timedelta(), "昨天": timedelta(days=1), "前天": timedelta(days=2)}
_REL_STEP = {"分钟": timedelta(minutes=1), "小时": timedelta(hours=1),
             "天": timedelta(days=1)}


def _norm_published_at(value: str, base: datetime | None = None) -> str:
    """相对时间（"9小时前"）→ 绝对日期 YYYY-MM-DD；已是日期或无法识别的原样返回。

    base 为换算参照时刻，默认当前时间。
    """
    s = (value or "").strip()
    if not s:
        return ""
    m = _REL_RE.match(s)
    if m:
        delta = _REL_STEP[m.group(2)] * int(m.group(1))
    elif s in _REL_FIXED:
        delta = _REL_FIXED[s]
    else:
        return s
    return ((base or datetime.now()) - delta).strftime("%Y-%m-%d")


def upsert_items(source: str, items) -> int:
    """保存/更新一批条目（Item 或 dict），按 info_hash 去重。

    返回本批新插入的条数（0 表示全部已存在，用于增量同步判断）。
    无磁力或 hash 非法的条目跳过。
    """
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for it in items:
        d = it.to_dict() if hasattr(it, "to_dict") else dict(it)
        magnet = (d.get("magnet") or "").strip()
        h = _hash_of(magnet)
        if not h:
            continue
        extra = d.get("extra") or {}
        trackers = extra.get("trackers") or []
        trackers_json = json.dumps(trackers, ensure_ascii=False) if trackers else ""
        rows.append((
            h, magnet,
            (d.get("title") or "").strip(),
            (d.get("size") or "").strip(),
            _norm_published_at(d.get("published_at"), now_dt),
            (d.get("category") or "").strip(),
            (d.get("detail_url") or "").strip(),
            source,
            (extra.get("torrent_name") or "").strip(),
            trackers_json,
            now, now,
        ))
    if not rows:
        return 0
    with _LOCK:
        with _db() as conn:
            # 先查已存在的 hash，得到本批真正新增的数量
            hashes = [r[0] for r in rows]
            ph = ",".join("?" * len(hashes))
            existing = {r[0] for r in conn.execute(
                f"SELECT info_hash FROM magnets WHERE info_hash IN ({ph})", hashes)}
            conn.executemany(
                """INSERT INTO magnets (info_hash, magnet, title, size, published_at,
                         category, detail_url, source, torrent_name, trackers,
                         first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(info_hash) DO UPDATE SET
                       last_seen_at=excluded.last_seen_at,
                       title=CASE WHEN excluded.title!='' THEN excluded.title ELSE magnets.title END,
                       size=CASE WHEN excluded.size!='' THEN excluded.size ELSE magnets.size END,
                       published_at=CASE WHEN excluded.published_at!=''
                           THEN excluded.published_at ELSE magnets.published_at END,
                       category=CASE WHEN excluded.category!='' THEN excluded.category ELSE magnets.category END,
                       detail_url=CASE WHEN excluded.detail_url!='' THEN excluded.detail_url ELSE magnets.detail_url END,
                       torrent_name=CASE WHEN excluded.torrent_name!=''
                           THEN excluded.torrent_name ELSE magnets.torrent_name END,
                       trackers=CASE WHEN excluded.trackers!='' THEN excluded.trackers ELSE magnets.trackers END""",
                rows,
            )
    return len({h for h in hashes if h not in existing})


def query_items(page: int = 1, keyword: str = "", page_size: int = 20):
    """查询本地库（按入库先后倒序）。keyword 匹配标题/种子名/hash/分类。

    返回 (rows, total)。
    """
    where, params = "1=1", []
    if keyword:
        where = "(title LIKE ? OR torrent_name LIKE ? OR info_hash LIKE ? OR category LIKE ?)"
        kw = f"%{keyword}%"
        params = [kw, kw, kw, kw]
    offset = (page - 1) * page_size
    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM magnets WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM magnets WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()
    return rows, total


_STATS_TTL = 15  # 秒：库统计缓存时长（避免高频轮询反复全表扫描）
_stats_cache: dict = {"t": 0.0, "data": None}


def get_stats() -> dict:
    """库统计：总数、分类分布、数据库文件大小、最近入库时间（15s 缓存）"""
    now = time.monotonic()
    cached = _stats_cache["data"]
    if cached is not None and now - _stats_cache["t"] < _STATS_TTL:
        return cached
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM magnets").fetchone()[0]
        by_cat = {r[0] or "未分类": r[1] for r in conn.execute(
            "SELECT category, COUNT(*) FROM magnets GROUP BY category")}
        last_seen = conn.execute(
            "SELECT MAX(last_seen_at) FROM magnets").fetchone()[0] or ""
    try:
        size_mb = round(_db_path().stat().st_size / 1048576, 1)
    except OSError:
        size_mb = 0
    result = {"total": total, "by_category": by_cat,
              "db_size_mb": size_mb, "last_seen": last_seen}
    _stats_cache.update(t=now, data=result)
    return result


# ---- 全量同步进度（断点续抓） ----

def set_sync_progress(source: str, section: int, page: int) -> None:
    with _LOCK:
        with _db() as conn:
            conn.execute(
                """INSERT INTO sync_state (key, last_page, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       last_page=excluded.last_page, updated_at=excluded.updated_at""",
                (f"{source}:{section}", page, _now()),
            )


def get_sync_progress(source: str) -> dict[int, int]:
    """返回 {板块代码: 已完成页码}（断点续抓用，不含 :done 完成标记）"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT key, last_page FROM sync_state WHERE key LIKE ? AND key NOT LIKE ?",
            (f"{source}:%", f"{source}:%:done"),
        ).fetchall()
    out: dict[int, int] = {}
    for r in rows:
        try:
            out[int(r["key"].split(":")[1])] = r["last_page"]
        except (IndexError, ValueError):
            continue
    return out


def clear_sync_progress(source: str, section: int) -> None:
    """删除某板块的断点（全量自然完成后调用，下次运行转增量模式）"""
    with _LOCK:
        with _db() as conn:
            conn.execute("DELETE FROM sync_state WHERE key = ?", (f"{source}:{section}",))


def set_sync_done(source: str, section: int) -> None:
    """标记某板块已完成全量同步（下次运行自动走增量更新）"""
    with _LOCK:
        with _db() as conn:
            conn.execute(
                """INSERT INTO sync_state (key, last_page, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at""",
                (f"{source}:{section}:done", 0, _now()),
            )


def get_sync_done(source: str) -> list[int]:
    """返回已完成全量同步的板块代码列表"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT key FROM sync_state WHERE key LIKE ?", (f"{source}:%:done",)
        ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(int(r["key"].split(":")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


# ---- 通用配置（键值对，供网页 UI 读写定时任务等设置） ----

def get_setting(key: str, default: str = "") -> str:
    with _db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _LOCK:
        with _db() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )
