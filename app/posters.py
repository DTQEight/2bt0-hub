"""影片海报本地化：把图床（如 tu.mvinfo.homes）上的海报下载到 DATA_DIR/posters。

为什么要本地存：海报图都在第三方图床，图床限流、防盗链或下线时，海报墙与
影片详情卡会整片空白。拉影片详情时顺手存一份到本地，之后由 /posters/{文件名}
直接提供，不再依赖外部图床。

扩展名按文件真实字节判断：实测图床把 WebP 图当 image/png 返回（URL 后缀也是
.png），只看后缀或 Content-Type 会存出"名不副实"的文件（本地服务发错 MIME）。
"""

from __future__ import annotations

import logging
import os
import time
import zlib
from pathlib import Path

import requests

from sources.bt0 import HEADERS as SITE_HEADERS

logger = logging.getLogger("resource-hub.posters")

# 单张海报上限：正常 30~500KB，超 5MB 视为异常响应（防资源耗尽）
MAX_BYTES = 5 * 1024 * 1024
RETRIES = 2
TIMEOUT = (15, 45)

_HEADERS = {**SITE_HEADERS, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}


def poster_dir() -> Path:
    """海报存放目录（随 DATA_DIR 持久化，容器重建不丢）"""
    d = Path(os.getenv("DATA_DIR", "/data")).resolve() / "posters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sniff_ext(data: bytes, url: str) -> str:
    """按文件头判断真实图片格式（图床会谎报类型，不能信后缀与 Content-Type）"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis", b"mif1"):
        return "avif"
    suffix = Path(url.split("?")[0]).suffix.lower().lstrip(".")
    return suffix if suffix in ("jpg", "jpeg", "png", "webp", "gif", "avif") else "jpg"


# 海报扩展名是固定的几种（见 _sniff_ext 与 /posters/ 的白名单）
_POSTER_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "avif")

# 分桶数量：库满约 9.3 万张海报，10 个桶每桶 ~9300 张，避免单目录
# 几十万文件（备份/浏览/文件系统检索都吃力）。idcode 跨度极大且分布
# 稀疏（3700 万跨度 7 万部影片），数值区间分桶会碎成上千个小目录，
# 哈希取模才能做到每桶均匀。注意不能用内置 hash()：字符串有进程级
# 随机化（PYTHONHASHSEED），跨进程不稳定；crc32 是确定的
SHARD_BUCKETS = 10


def shard_dir(idcode: str) -> Path:
    """该影片海报所在的分桶子目录（posters/00 ~ posters/09，纯计算不建目录）"""
    return poster_dir() / f"{zlib.crc32(idcode.encode()) % SHARD_BUCKETS:02d}"


def find(idcode: str) -> str:
    """该影片已缓存的海报文件名（未缓存返回空串）。

    先查分桶目录（新位置），再查根目录（分桶迁移前的旧位置，兼容
    迁移进行中/中断的情况）。直接按扩展名逐个 stat：Path.glob 要
    枚举整个目录做名称匹配，海报目录涨到几十万文件后单次未命中查询
    要几十毫秒（stat 仅 0.05ms），详情批量拉取时每部影片都要查一次，
    glob 会把任务拖慢数小时。
    """
    if not idcode:
        return ""
    for d in (shard_dir(idcode), poster_dir()):
        for ext in _POSTER_EXTS:
            p = d / f"{idcode}.{ext}"
            if p.is_file():
                return p.name
    return ""


def resolve(name: str) -> Path | None:
    """按海报文件名定位文件（分桶目录优先、根目录兜底），供 /posters/ 路由用。

    URL 形如 /posters/{idcode}.{ext}（历史格式不变），文件实际落在
    分桶子目录里，由这里的哈希推导还原路径。
    """
    idcode = name.split(".")[0]
    for d in (shard_dir(idcode), poster_dir()):
        p = d / name
        if p.is_file():
            return p
    return None


def migrate_to_shards() -> int:
    """把历史散在 posters 根目录的海报移入分桶子目录。

    幂等：已在桶里的不动，根目录清空后再跑等于空操作。
    容器启动时后台调用，NAS 上的旧数据首次更新镜像后自动完成迁移。
    """
    root = poster_dir()
    moved = dedup = 0
    for p in root.iterdir():
        if not p.is_file() or p.suffix.lstrip(".").lower() not in _POSTER_EXTS:
            continue  # .part 临时文件、日志等不参与
        dest_dir = shard_dir(p.name.split(".")[0])
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / p.name
        try:
            if dest.exists():
                p.unlink()  # 桶里已有同名（idcode 唯一，视为同一张）
                dedup += 1
            else:
                p.replace(dest)
                moved += 1
        except OSError:
            logger.warning("海报迁移失败：%s", p.name)
    if moved or dedup:
        logger.info("海报分桶迁移完成：移动 %d 张，去重删除 %d 张", moved, dedup)
    return moved


def _download(url: str) -> bytes:
    """下载海报，失败返回空字节串（只记日志，不打断详情拉取）"""
    for attempt in range(RETRIES + 1):
        try:
            with requests.get(url, headers=_HEADERS, timeout=TIMEOUT,
                              verify=False, stream=True) as r:  # 与站点同款：证书链不完整
                if r.status_code != 200:
                    raise requests.RequestException(f"HTTP {r.status_code}")
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise ValueError(f"海报超过 {MAX_BYTES // 1048576}MB 上限，已丢弃")
                if not buf:
                    raise ValueError("空响应")
                return bytes(buf)
        except Exception as exc:
            if attempt < RETRIES:
                time.sleep(1.0 * (attempt + 1))
                continue
            logger.warning("海报下载失败 %s: %s", url, exc)
    return b""


def ensure(idcode: str, url: str) -> str:
    """确保该影片的海报已存到本地，返回文件名（无需下载或失败时返回空串）。

    已缓存过就直接复用，不重复下载。
    """
    if not idcode or not url:
        return ""
    cached = find(idcode)
    if cached:
        return cached
    if not url.lower().startswith(("http://", "https://")):
        return ""
    data = _download(url)
    if not data:
        return ""
    name = f"{idcode}.{_sniff_ext(data, url)}"
    dest_dir = shard_dir(idcode)
    dest_dir.mkdir(exist_ok=True)
    target = dest_dir / name
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)  # 原子替换，避免读到半张图
    except OSError as exc:
        logger.warning("海报落盘失败 %s: %s", name, exc)
        tmp.unlink(missing_ok=True)
        return ""
    return name
