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
    movie_id TEXT DEFAULT '',
    movie_title TEXT DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
-- 影片表：站点 getVideoDetail 接口的影片维度元数据，按 idcode 唯一。
-- 一部影片对应多条种子（magnets.movie_id），故影片级字段不重复存在种子上。
CREATE TABLE IF NOT EXISTS movies (
    idcode TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    otitle TEXT DEFAULT '',
    alias TEXT DEFAULT '',
    years TEXT DEFAULT '',
    category TEXT DEFAULT '',
    area TEXT DEFAULT '',
    language TEXT DEFAULT '',
    episodes TEXT DEFAULT '',
    long_time TEXT DEFAULT '',
    doub_score TEXT DEFAULT '',
    imdb_id TEXT DEFAULT '',
    imdb_score TEXT DEFAULT '',
    director TEXT DEFAULT '',
    performer TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    fetched_at TEXT NOT NULL
);
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

# 索引单独建：idx_magnets_movie 依赖 movie_id 列，必须在旧库补列之后执行
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_magnets_id ON magnets(id);
CREATE INDEX IF NOT EXISTS idx_magnets_category ON magnets(category);
CREATE INDEX IF NOT EXISTS idx_magnets_last_seen ON magnets(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_magnets_movie ON magnets(movie_id);
-- (category, movie_id)：按片名分组浏览时通常带分类过滤，命中它可省掉 GROUP BY 的临时 B 树
-- （82 万行实测：加索引前 5.6 秒，加后 0.35 秒）
CREATE INDEX IF NOT EXISTS idx_magnets_cat_movie ON magnets(category, movie_id);
-- 索引用途说明：
--   id               → 无分类过滤时分页"定位本页首行"（覆盖索引，只扫约 10MB；否则深页要全表扫约 300MB）
--   category         → get_stats() 的 GROUP BY category，以及本地库按分类筛选后分页定位本页首行
--                      （id 即 rowid，故该单列索引对 SELECT id 是覆盖索引）
--   last_seen_at     → get_stats() 的 MAX(last_seen_at)
--   movie_id         → "该片的全部版本"分页定位（idx_magnets_movie 对 SELECT id 是覆盖索引）
--   (category,movie_id) → 按片名分组浏览（GROUP BY movie_id，带分类过滤）
-- 原先还有 title / source 两个索引，但现有查询用不上：关键词检索是
-- LIKE '%kw%'（前置通配符无法走 B-tree 索引），也没有按 source 过滤的语句。
-- 二者在 82 万行时占用约 93MB 并拖慢每行写入，故不再创建，并清理存量。
DROP INDEX IF EXISTS idx_magnets_title;
DROP INDEX IF EXISTS idx_magnets_source;
"""


def init_db() -> None:
    with _LOCK:
        with _db() as conn:
            conn.executescript(_SCHEMA)
            # CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，旧库需显式迁移
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(magnets)")}
            for name in ("movie_id", "movie_title"):
                if name not in cols:
                    conn.execute(f"ALTER TABLE magnets ADD COLUMN {name} TEXT DEFAULT ''")
            conn.executescript(_INDEXES)


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
            (extra.get("movie_id") or "").strip(),
            (extra.get("movie_title") or "").strip(),
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
                         movie_id, movie_title, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                       trackers=CASE WHEN excluded.trackers!='' THEN excluded.trackers ELSE magnets.trackers END,
                       movie_id=CASE WHEN excluded.movie_id!='' THEN excluded.movie_id ELSE magnets.movie_id END,
                       movie_title=CASE WHEN excluded.movie_title!=''
                           THEN excluded.movie_title ELSE magnets.movie_title END""",
                rows,
            )
    return len({h for h in hashes if h not in existing})


def query_items(page: int = 1, keyword: str = "", category: str = "",
                movie_id: str = "", page_size: int = 20):
    """查询本地库（按入库先后倒序）。

    keyword 模糊匹配标题/种子名/hash/分类；category 为精确分类过滤（如"电影"）；
    movie_id 过滤某部影片的全部版本。返回 (rows, total)。
    """
    conds, params = [], []
    if category:
        conds.append("category = ?")
        params.append(category)
    if movie_id:
        conds.append("movie_id = ?")
        params.append(movie_id)
    if keyword:
        conds.append(
            "(title LIKE ? OR torrent_name LIKE ? OR info_hash LIKE ? OR category LIKE ?)")
        kw = f"%{keyword}%"
        params += [kw, kw, kw, kw]
    where = " AND ".join(conds) or "1=1"
    offset = (page - 1) * page_size
    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM magnets WHERE {where}", params).fetchone()[0]
        if keyword:
            # 关键词是 LIKE '%kw%'，无法走索引，两步法反而要多扫一遍，保持单次查询
            rows = conn.execute(
                f"SELECT * FROM magnets WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, page_size, offset],
            ).fetchall()
        else:
            # 两步分页：先用索引定位本页首行（无分类过滤走 id 覆盖索引，
            # 有分类过滤走 category 索引），再按 id 范围取整行。
            # 直接 LIMIT/OFFSET 会让 SQLite 全表扫描并逐行丢弃，深页要读约 300MB。
            anchor = conn.execute(
                f"SELECT id FROM magnets WHERE {where} ORDER BY id DESC LIMIT 1 OFFSET ?",
                [*params, offset],
            ).fetchone()
            rows = [] if anchor is None else conn.execute(
                f"SELECT * FROM magnets WHERE {where} AND id <= ? ORDER BY id DESC LIMIT ?",
                [*params, anchor[0], page_size],
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


# ---- 影片（movies）：站点 getVideoDetail 接口的影片维度元数据 ----

# 与 movies 表列一一对应（idcode 是主键，其余为详情字段）
_MOVIE_FIELDS = ("idcode", "title", "otitle", "alias", "years", "category", "area",
                 "language", "episodes", "long_time", "doub_score", "imdb_id",
                 "imdb_score", "director", "performer", "abstract")


def upsert_movies(rows: list[dict]) -> int:
    """保存/更新一批影片详情，按 idcode 覆盖。返回写入条数。"""
    values = [tuple(str(r.get(f) or "").strip() for f in _MOVIE_FIELDS) + (_now(),)
              for r in rows if str(r.get("idcode") or "").strip()]
    if not values:
        return 0
    cols = ", ".join(_MOVIE_FIELDS) + ", fetched_at"
    ph = ",".join("?" * (len(_MOVIE_FIELDS) + 1))
    updates = ", ".join(f"{f}=excluded.{f}" for f in _MOVIE_FIELDS[1:])
    with _LOCK:
        with _db() as conn:
            conn.executemany(
                f"""INSERT INTO movies ({cols}) VALUES ({ph})
                    ON CONFLICT(idcode) DO UPDATE SET {updates},
                        fetched_at=excluded.fetched_at""",
                values,
            )
    return len(values)


def pending_movie_ids() -> list[str]:
    """种子里出现过、但 movies 表还没有详情的影片 id（供批量拉取详情）。"""
    with _db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT m.movie_id FROM magnets m
               LEFT JOIN movies v ON v.idcode = m.movie_id
               WHERE m.movie_id != '' AND v.idcode IS NULL""").fetchall()
    return [r[0] for r in rows]


def get_movie(idcode: str):
    """取一部影片的详情（无则返回 None）"""
    with _db() as conn:
        row = conn.execute("SELECT * FROM movies WHERE idcode = ?", (idcode,)).fetchone()
    return dict(row) if row else None


_movie_stats_cache: dict = {"t": 0.0, "data": None}


def movie_stats() -> dict:
    """影片维度统计：已入库影片数、种子里涉及的去重影片数（15s 缓存）"""
    now = time.monotonic()
    cached = _movie_stats_cache["data"]
    if cached is not None and now - _movie_stats_cache["t"] < _STATS_TTL:
        return cached
    with _db() as conn:
        fetched = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        wanted = conn.execute(
            "SELECT COUNT(DISTINCT movie_id) FROM magnets WHERE movie_id > ''"
        ).fetchone()[0]
    result = {"fetched": fetched, "wanted": wanted}
    _movie_stats_cache.update(t=now, data=result)
    return result


def query_groups(page: int = 1, keyword: str = "", category: str = "",
                 page_size: int = 20):
    """按影片分组浏览本地库：每组＝一部影片及其版本数。

    组按"该片最新入库的种子"倒序。返回 (rows, total_groups)。
    """
    conds, params = ["movie_id > ''"], []
    if category:
        conds.append("category = ?")
        params.append(category)
    if keyword:
        conds.append("(movie_title LIKE ? OR title LIKE ? OR movie_id LIKE ?)")
        kw = f"%{keyword}%"
        params += [kw, kw, kw]
    where = " AND ".join(conds)
    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(DISTINCT movie_id) FROM magnets WHERE {where}",
            params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT movie_id, MAX(movie_title) AS movie_title,
                       COUNT(*) AS versions, MAX(id) AS last_id
                FROM magnets WHERE {where}
                GROUP BY movie_id ORDER BY last_id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size]).fetchall()
    return rows, total


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


def backup_to(target: Path) -> None:
    """把整个库复制到 target（VACUUM INTO）。

    生成的是紧凑副本（等价于压缩 + 去碎片），且是一次只读快照，
    可在服务运行时执行，不影响并发读写。
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # VACUUM 不能在事务内执行，因此用自动提交（isolation_level=None）连接
    conn = sqlite3.connect(str(_db_path()), timeout=60, isolation_level=None)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    with _LOCK:
        with _db() as conn:
            conn.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, _now()),
            )
