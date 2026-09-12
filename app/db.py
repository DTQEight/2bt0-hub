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
    doub_votes TEXT DEFAULT '',
    imdb_id TEXT DEFAULT '',
    imdb_score TEXT DEFAULT '',
    imdb_votes TEXT DEFAULT '',
    image TEXT DEFAULT '',
    director TEXT DEFAULT '',
    performer TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    definition TEXT DEFAULT '',
    detail_ver INTEGER DEFAULT 0,
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
            vcols = {r["name"] for r in conn.execute("PRAGMA table_info(movies)")}
            for name in ("doub_votes", "imdb_votes", "image", "tags", "definition"):
                if name not in vcols:
                    conn.execute(f"ALTER TABLE movies ADD COLUMN {name} TEXT DEFAULT ''")
            # detail_ver：详情字段版本。补 tags/definition 之前的旧记录为 0，
            # 会被计入「待拉取」重新抓一次（否则旧片永远筛不出标签和画质）
            if "detail_ver" not in vcols:
                conn.execute("ALTER TABLE movies ADD COLUMN detail_ver INTEGER DEFAULT 0")
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
        kw = f"%{keyword}%"
        # 除种子自身字段外，还匹配影片的原名/别名/导演/主演（存在 movies 表）
        conds.append(
            "(title LIKE ? OR torrent_name LIKE ? OR info_hash LIKE ? OR category LIKE ?"
            " OR movie_id IN (SELECT idcode FROM movies"
            "                 WHERE otitle LIKE ? OR alias LIKE ?"
            "                    OR performer LIKE ? OR director LIKE ?))")
        params += [kw] * 8
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
                 "language", "episodes", "long_time", "doub_score", "doub_votes",
                 "imdb_id", "imdb_score", "imdb_votes", "image",
                 "director", "performer", "abstract", "tags", "definition")

# 详情数据版本：加了 tags/definition 之后递增，库内低于此值的记录会重新拉取
DETAIL_VER = 1


def upsert_movies(rows: list[dict]) -> int:
    """保存/更新一批影片详情，按 idcode 覆盖。返回写入条数。"""
    values = [tuple(str(r.get(f) or "").strip() for f in _MOVIE_FIELDS) + (_now(), DETAIL_VER)
              for r in rows if str(r.get("idcode") or "").strip()]
    if not values:
        return 0
    cols = ", ".join(_MOVIE_FIELDS) + ", fetched_at, detail_ver"
    ph = ",".join("?" * (len(_MOVIE_FIELDS) + 2))
    updates = ", ".join(f"{f}=excluded.{f}" for f in _MOVIE_FIELDS[1:])
    with _LOCK:
        with _db() as conn:
            conn.executemany(
                f"""INSERT INTO movies ({cols}) VALUES ({ph})
                    ON CONFLICT(idcode) DO UPDATE SET {updates},
                        fetched_at=excluded.fetched_at,
                        detail_ver=excluded.detail_ver""",
                values,
            )
    return len(values)


def pending_movie_ids(category: str = "") -> list[str]:
    """种子里出现过、但详情没拉过（或版本过旧需重拉）的影片 id。

    category 非空时只取该分类（电影/电视剧）的影片，用于按板块分开拉详情。
    """
    sql = ("SELECT DISTINCT m.movie_id FROM magnets m"
           " LEFT JOIN movies v ON v.idcode = m.movie_id"
           " WHERE m.movie_id != '' AND (v.idcode IS NULL OR v.detail_ver < ?)")
    params: list = [DETAIL_VER]
    if category:
        sql += " AND m.category = ?"
        params.append(category)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def get_movie(idcode: str):
    """取一部影片的详情（无则返回 None）"""
    with _db() as conn:
        row = conn.execute("SELECT * FROM movies WHERE idcode = ?", (idcode,)).fetchone()
    return dict(row) if row else None


_movie_stats_cache: dict = {}  # category(或 "" 表示全部) → (monotonic, data)


def movie_stats(category: str = "") -> dict:
    """影片维度统计：种子里涉及的去重影片数、已入库详情数、待拉取数（15s 缓存）。

    category 非空时只统计该分类（电影/电视剧），供板块卡片分别显示详情进度。
    """
    now = time.monotonic()
    hit = _movie_stats_cache.get(category)
    if hit is not None and now - hit[0] < _STATS_TTL:
        return hit[1]
    # 待拉取 = 没拉过，或拉过的详情版本过旧（缺 tags/definition）需重拉
    stale = f"(v.idcode IS NULL OR v.detail_ver < {DETAIL_VER})"
    if category:
        wanted_sql = ("SELECT COUNT(DISTINCT movie_id) FROM magnets"
                      " WHERE movie_id > '' AND category = ?")
        wanted_params: list = [category]
        pending_sql = ("SELECT COUNT(DISTINCT m.movie_id) FROM magnets m"
                       " LEFT JOIN movies v ON v.idcode = m.movie_id"
                       f" WHERE m.movie_id > '' AND m.category = ? AND {stale}")
        pending_params: list = [category]
    else:
        wanted_sql = "SELECT COUNT(DISTINCT movie_id) FROM magnets WHERE movie_id > ''"
        wanted_params = []
        pending_sql = ("SELECT COUNT(DISTINCT m.movie_id) FROM magnets m"
                       " LEFT JOIN movies v ON v.idcode = m.movie_id"
                       f" WHERE m.movie_id > '' AND {stale}")
        pending_params = []
    with _db() as conn:
        wanted = conn.execute(wanted_sql, wanted_params).fetchone()[0]
        pending = conn.execute(pending_sql, pending_params).fetchone()[0]
    # 待拉取的必然也在 wanted 里，相减即已入库，省一次全表 COUNT
    result = {"fetched": wanted - pending, "wanted": wanted, "pending": pending}
    _movie_stats_cache[category] = (now, result)
    return result


def invalidate_movie_stats() -> None:
    """清空影片统计与筛选选项缓存（详情批量入库后调用，避免页面继续显示旧数字）"""
    _movie_stats_cache.clear()
    _filter_options_cache.clear()


# 海报墙排序：值 → ORDER BY 表达式。last/versions 取自分组结果，可以先分页再
# 连 movies（只连本页 24 行）；score/years/votes 在 movies 表里，必须先连接全部组再排序。
_GROUP_SORT_EXPR = {
    "last": "last_id",
    "versions": "versions",
    "score": "CAST(m.doub_score AS REAL)",
    "years": "CAST(m.years AS INTEGER)",
    "votes": "CAST(m.doub_votes AS INTEGER)",
}
_SORTS_NEEDING_MOVIES = {"score", "years", "votes"}


# ---- 筛选条（分类参考主站 2bt0.com 影片库筛选，去掉「仅显示网盘资源」）----

# 五组标签及先后顺序，与站点 getVideoTypeList 的 t1~t5 一致
FILTER_GROUPS = (
    ("ftype", "影视类型", ("喜剧", "剧情", "动作", "爱情", "科幻", "动画", "悬疑", "惊悚",
                       "恐怖", "犯罪", "同性", "音乐", "歌舞", "传记", "历史", "战争",
                       "西部", "奇幻", "冒险", "灾难", "武侠", "真人秀", "纪录片")),
    ("farea", "制片地区", ("大陆", "欧美", "美国", "香港", "台湾", "日本", "韩国", "英国",
                       "法国", "德国", "西班牙", "印度", "泰国", "俄罗斯", "加拿大",
                       "澳大利亚", "瑞典", "巴西")),
    ("fyears", "上映年份", ("近三年", "2026", "2025", "2024", "2023", "2022", "2021", "2020",
                        "2019", "2018", "2017", "20年代", "10年代", "00年代", "90年代",
                        "80年代", "更早")),
    ("fquality", "资源画质", ("HDTV", "WEB-1080P", "WEB-4K", "1080P蓝光", "1080P-Remux",
                          "4K蓝光", "4K-Remux", "3D", "杜比视界", "蓝光原盘",
                          "4K蓝光原盘", "枪版")),
    ("ftag", "影视标签", ("心理", "冷门", "人性", "丧尸", "搞笑", "吸血鬼", "温情", "魔幻")),
)

# 站点标签「大陆/香港/台湾」在库里存的是全称；「欧美」是聚合标签，按一组国家匹配
_AREA_RENAME = {"大陆": "中国大陆", "香港": "中国香港", "台湾": "中国台湾"}
_AREA_ALIAS = {v: k for k, v in _AREA_RENAME.items()}
_AREA_EUROPE = ("美国", "英国", "法国", "德国", "意大利", "西班牙", "葡萄牙", "荷兰",
                "比利时", "瑞士", "奥地利", "瑞典", "挪威", "丹麦", "芬兰", "波兰",
                "爱尔兰", "卢森堡", "希腊", "捷克", "俄罗斯", "加拿大", "澳大利亚",
                "新西兰")
_TERM_SPLIT = re.compile(r"[,，、]+")


def _split_terms(value: str) -> list[str]:
    """拆分逗号/顿号分隔的多值字段（类型、地区、画质、标签都是这种格式）"""
    return [t.strip() for t in _TERM_SPLIT.split(value or "") if t.strip()]


def _filter_cond(key: str, value) -> tuple[str, list] | None:
    """把一个筛选项翻译成 movies 表的 WHERE 条件（无法识别时返回 None）"""
    if key == "ftype":
        return "category LIKE ?", [f"%{value}%"]
    if key == "farea":
        if value == "欧美":
            return ("(" + " OR ".join("area LIKE ?" for _ in _AREA_EUROPE) + ")",
                    [f"%{a}%" for a in _AREA_EUROPE])
        return "area LIKE ?", [f"%{_AREA_RENAME.get(str(value), value)}%"]
    if key == "fquality":
        return "definition LIKE ?", [f"%{value}%"]
    if key == "ftag":
        return "tags LIKE ?", [f"%{value}%"]
    if key == "fyears":
        s = str(value)
        if s == "近三年":
            return "CAST(years AS INTEGER) >= ?", [datetime.now().year - 2]
        if s == "更早":
            # 0 是空值/非数字年份转出来的，需要排除
            return "CAST(years AS INTEGER) < 1980 AND CAST(years AS INTEGER) > 0", []
        if re.fullmatch(r"\d{4}", s):
            return "years LIKE ?", [f"{s}%"]
        m = re.fullmatch(r"(\d{2})年代", s)
        if m:
            start = int(m.group(1))
            start = start + 1900 if start >= 30 else start + 2000
            return "CAST(years AS INTEGER) BETWEEN ? AND ?", [start, start + 9]
    elif key == "votes_min":
        return "CAST(doub_votes AS INTEGER) >= ?", [int(value)]
    elif key == "score_min":
        return "CAST(doub_score AS REAL) >= ?", [float(value)]
    elif key == "score_max":
        return "CAST(doub_score AS REAL) <= ?", [float(value)]
    elif key == "imdb_only":
        return "imdb_score != '' AND imdb_score != '0'", []
    return None


def query_groups(page: int = 1, keyword: str = "", category: str = "",
                 page_size: int = 20, sort: str = "last", filters: dict | None = None):
    """按影片分组浏览本地库：每组＝一部影片及其版本数。

    sort：last=最新入库（默认）/ score=豆瓣评分 / votes=评分人数 / years=年份 /
          versions=版本数。
    filters：筛选条条件（ftype/farea/fyears/fquality/ftag/votes_min/score_min/
            score_max/imdb_only），值来自文件名前缀，除高级筛选项外都是标签文字。
    海报、年份、评分取自 movies 表，未拉取详情的影片这几个字段为空（前端占位）。
    keyword 除片名/种子名/影片 id 外，还匹配影片的原名、别名、导演、主演。
    返回 (rows, total_groups)。
    """
    conds, params = ["movie_id > ''"], []
    if category:
        conds.append("category = ?")
        params.append(category)
    # 影片级条件（类型/地区/年份/画质/标签/评分…）都落在 movies 表，用非相关
    # IN 子查询过滤，SQLite 只会求值一次并建临时索引，不影响分组扫描
    mconds, mparams = [], []
    for key, value in (filters or {}).items():
        if not value:
            continue
        cond = _filter_cond(key, value)
        if cond:
            mconds.append(cond[0])
            mparams += cond[1]
    if mconds:
        conds.append("movie_id IN (SELECT idcode FROM movies WHERE "
                     + " AND ".join(mconds) + ")")
        params += mparams
    if keyword:
        kw = f"%{keyword}%"
        # 原名/别名/导演/主演存在 movies 表里。用非相关 IN 子查询：SQLite 只会
        # 求值一次并建临时索引，比逐行 EXISTS 相关子查询少一轮索引查找。
        conds.append(
            "(movie_title LIKE ? OR title LIKE ? OR movie_id LIKE ?"
            " OR movie_id IN (SELECT idcode FROM movies"
            "                 WHERE otitle LIKE ? OR alias LIKE ?"
            "                    OR performer LIKE ? OR director LIKE ?))")
        params += [kw] * 7
    where = " AND ".join(conds)
    sort = sort if sort in _GROUP_SORT_EXPR else "last"
    group_sql = (f"SELECT movie_id, MAX(movie_title) AS movie_title,"
                 f"       COUNT(*) AS versions, MAX(id) AS last_id"
                 f" FROM magnets WHERE {where} GROUP BY movie_id")
    fields = ("g.movie_id, COALESCE(NULLIF(m.title, ''), g.movie_title) AS title,"
              " g.versions, m.image, m.years, m.doub_score")
    with _db() as conn:
        total = conn.execute(
            f"SELECT COUNT(DISTINCT movie_id) FROM magnets WHERE {where}",
            params).fetchone()[0]
        if sort in _SORTS_NEEDING_MOVIES:
            rows = conn.execute(
                f"""SELECT {fields} FROM ({group_sql}) g
                    LEFT JOIN movies m ON m.idcode = g.movie_id
                    ORDER BY {_GROUP_SORT_EXPR[sort]} DESC, g.last_id DESC
                    LIMIT ? OFFSET ?""",
                [*params, page_size, (page - 1) * page_size]).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT {fields} FROM (
                        {group_sql}
                        ORDER BY {_GROUP_SORT_EXPR[sort]} DESC, last_id DESC
                        LIMIT ? OFFSET ?) g
                    LEFT JOIN movies m ON m.idcode = g.movie_id""",
                [*params, page_size, (page - 1) * page_size]).fetchall()
    return rows, total


_FILTER_TTL = 120  # 秒：筛选选项缓存时长（聚合要扫一遍影片表）
_filter_options_cache: dict = {}


def filter_options(category: str = "") -> dict:
    """筛选条的可选项：只列出本地库确实有数据的标签，按站点顺序排列。

    category 为空时统计全部影片。没拉过详情的影片没有这些字段，自然不会出现在
    选项里，所以每个选项点下去都能出结果；站点分类之外、库里确实存在的值补在末尾。
    """
    now = time.monotonic()
    hit = _filter_options_cache.get(category)
    if hit is not None and now - hit[0] < _FILTER_TTL:
        return hit[1]

    counters: dict[str, dict[str, int]] = {key: {} for key, _, _ in FILTER_GROUPS}
    # 只统计被种子引用过的影片，避免把没有磁力的影片算进选项
    sql = ("SELECT v.category, v.area, v.years, v.definition, v.tags FROM movies v"
           " WHERE v.idcode IN (SELECT DISTINCT movie_id FROM magnets"
           "                    WHERE movie_id > ''")
    params: list = []
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += ")"
    with _db() as conn:
        for row in conn.execute(sql, params):
            for term in _split_terms(row["category"]):
                counters["ftype"][term] = counters["ftype"].get(term, 0) + 1
            for term in _split_terms(row["area"]):
                term = _AREA_ALIAS.get(term, term)  # 中国大陆 → 大陆（站点口径）
                counters["farea"][term] = counters["farea"].get(term, 0) + 1
            for term in _split_terms(row["definition"]):
                counters["fquality"][term] = counters["fquality"].get(term, 0) + 1
            for term in _split_terms(row["tags"]):
                counters["ftag"][term] = counters["ftag"].get(term, 0) + 1
            m = re.match(r"(\d{4})", (row["years"] or "").strip())
            if m:
                counters["fyears"][m.group(1)] = counters["fyears"].get(m.group(1), 0) + 1

    # 年份里有几个区间派生标签（近三年 / X0年代 / 更早），按实际年份折算是否可选
    years = {int(k): n for k, n in counters["fyears"].items() if k.isdigit()}
    for decade in (2020, 2010, 2000, 1990, 1980):
        counters["fyears"][f"{decade // 10 % 10 * 10}年代"] = sum(
            n for y, n in years.items() if decade <= y <= decade + 9)
    counters["fyears"]["近三年"] = sum(
        n for y, n in years.items() if y >= datetime.now().year - 2)
    counters["fyears"]["更早"] = sum(n for y, n in years.items() if y < 1980)
    # 「欧美」同样是聚合口径：库里只要有这些国家的影片即可选
    counters["farea"]["欧美"] = sum(counters["farea"].get(a, 0) for a in _AREA_EUROPE)

    groups = []
    for key, label, std in FILTER_GROUPS:
        counts = counters[key]
        options = [t for t in std if counts.get(t)]
        # 站点分类之外、库里确实存在的值（如画质「其他」、标签「经典」）补在末尾；
        # 年份不补：库里每个具体年份都能成按钮的话，这一行会长到没法看
        if key != "fyears":
            options += sorted((t for t, n in counts.items() if n and t not in std),
                              key=lambda t: (-counts[t], t))[:10]
        groups.append({"key": key, "label": label, "options": options})
    result = {"groups": groups}
    _filter_options_cache[category] = (now, result)
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
