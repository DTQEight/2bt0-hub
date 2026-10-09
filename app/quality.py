"""种子画质分桶：把种子标题归到一个人能看懂的画质标签（电影详情页筛选用）。

**作用范围**：电影详情页的磁力列表（这部片子的版本），按需计算、不落库。
一部影片平均 8.8 个版本（实测：3.5 万部只有 1 个版本，最长尾的电视剧 2117 个），
每次查询对这部影片的几十行跑一遍分类即可，毫秒级。若将来要让全库 82 万条磁力
也能按画质筛，才需要「加列 + 索引 + 回填」，那时再改造。

为什么需要它：站点只在标题里写 `BluRay.REMUX.1080p.Atmos`、`2160p.WEB-DL.H265`
这种发布组命名，用户想「只看 4K」或「只看蓝光」时没法筛。这里按业界命名习惯
把每个种子归到一个桶。

规则设计（踩过的坑都记在下面，改规则前先读）：
- **先判来源、再判分辨率**：标题常同时含多个词（`Bluray.1080p`、
  `UHD.BluRay.REMUX.2160p`），单纯按关键词优先级串匹配会把 720p 的 WEB 判成
  4K、把 1080p 的 WEBRip 判成蓝光。所以每步都同时看「来源 × 分辨率」。
- **`dv` 不能当杜比视界**：独立 `dv` 会命中 `DVD`（实测独立 `dv` 19,403 条里
  绝大部分是它），只认 `dovi` / `dolby vision` / 中文「杜比视界」。
- **`ts` / `tc` 不能当抢版**：`ts` 会命中 `DTS`（十万级误伤），只认中文「枪版」与
  `HDTS/HDCAM/HDTC/CAMRIP/TELESYNC` 这类完整写法。
- **`iso` 必须带边界**：`iso` 若按子串匹配会命中 `Prison`/`Prisoner`/`Tristan`
  （`prison` 里含 `iso`），实测把上千条 WEB-DL 错判成「蓝光原盘」，
  故用 `(?<![a-z0-9])iso(?![a-z0-9])`。
- **`4K` 与 `2160p` 不是一回事**：`2160p` 15 万条是真 4K，独立 `4K` 只有 979 条、
  多数是「4K 修复版」（片子本身仍是 1080p）；两者都算 4K，但优先级低于原盘/Remux。
- 中文「蓝光/原盘」在该站极少（原盘仅百余条，且多为 1080p 蓝光原盘），
  所以「4K蓝光原盘」这类桶接近空桶，前端按 count>0 自动隐藏。

`classify()` 是唯一规则来源；`register_udf()` 把它注册成 SQLite 函数，
SQL 侧（筛选与计数）直接调用，避免出现第二套规则实现。
"""

from __future__ import annotations

import re
import sqlite3

# ---- 各维度正则片段（对 title 先 replace("_", ".") 再 lower() 后匹配）----
R_2160 = r"2160p|(?<![a-z0-9])4k(?![a-z0-9])|uhd"
R_1080 = r"1080[pi]"
R_720 = r"720p"
S_DISC = r"原盘|bdmv|(?<![a-z0-9])iso(?![a-z0-9])"        # 蓝光原盘/镜像
S_REMUX = r"remux"                                        # 原盘无损重封装
S_BLURAY = r"blu[ ._-]?ray|bdrip|blu(?![a-z0-9])|蓝光"      # 蓝光压制/重编码
S_HDTV = r"hdtv"                                          # 电视台录制
S_CAM = r"枪版|抢先版|(?<![a-z0-9])(hdts|hdcam|hdtc|camrip|telesync)(?![a-z0-9])"
S_DVD = r"dvdrip|dvdscr|(?<![a-z0-9])dvd(?![a-z0-9])|(?<![a-z0-9])hdvd(?![a-z0-9])"
S_3D = r"3d|hsbs|half[ ._-]?sbs|(?<![a-z0-9])sbs(?![a-z0-9])|左右半宽|上下半高"
S_DV = r"dovi|dolby[ ._-]?vision|杜比视界"

OTHER = "其他"

# ---- 分桶规则：自上而下取第一个命中的桶（顺序 = 筛选条展示顺序，画质从高到低）----
# no_720=True：标题里出现 720p 就不落这个桶（否则"720p.BluRay"会被算成 1080P 蓝光）
RULES: list[tuple[str, str, bool]] = [
    ("3D",          S_3D,                                                      False),
    ("杜比视界",     S_DV,                                                      False),
    ("4K蓝光原盘",   f"(?:{R_2160}).*(?:{S_DISC})|(?:{S_DISC}).*(?:{R_2160})",    False),
    ("蓝光原盘",     S_DISC,                                                    True),
    ("4K-Remux",    f"(?:{R_2160}).*(?:{S_REMUX})|(?:{S_REMUX}).*(?:{R_2160})",  False),
    ("1080P-Remux", S_REMUX,                                                   True),
    ("4K蓝光",       f"(?:{R_2160}).*(?:{S_BLURAY})|(?:{S_BLURAY}).*(?:{R_2160})", False),
    ("1080P蓝光",    f"(?:{S_BLURAY}).*(?:{R_1080})|(?:{R_1080}).*(?:{S_BLURAY})", False),
    ("WEB-4K",      R_2160,                                                    False),
    ("WEB-1080P",   R_1080,                                                    False),
    ("HDTV",        S_HDTV,                                                    True),
    ("1080P",       R_1080,                                                    True),
    ("720P",        R_720,                                                     False),
    ("标清",         S_DVD,                                                     False),
    ("枪版",         S_CAM,                                                     False),
]

# 筛选条展示顺序（含兜底桶）
BUCKETS: tuple[str, ...] = tuple(name for name, _, _ in RULES) + (OTHER,)

_COMPILED = tuple((name, re.compile(pat, re.I), no720) for name, pat, no720 in RULES)
_R_720 = re.compile(R_720, re.I)


def classify(title: str) -> str:
    """把种子标题归到一个画质桶；无法判断时返回「其他」。"""
    t = (title or "").replace("_", ".").lower()
    has_720 = bool(_R_720.search(t))
    for name, rx, no720 in _COMPILED:
        if no720 and has_720:
            continue
        if rx.search(t):
            return name
    return OTHER


def register_udf(conn: sqlite3.Connection, name: str = "quality_bucket") -> None:
    """把 classify 注册成 SQLite 标量函数，让 SQL 侧复用同一套规则。

    调用后可在 SQL 里 `quality_bucket(title)`，用于 WHERE 过滤与 GROUP BY 计数。
    deterministic=True 让 SQLite 能缓存同参数结果（同标题只算一次）。
    """
    conn.create_function(
        name, 1,
        lambda title: classify(title if isinstance(title, str) else ""),
        deterministic=True)
