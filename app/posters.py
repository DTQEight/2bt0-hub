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


def find(idcode: str) -> str:
    """该影片已缓存的海报文件名（未缓存返回空串）。

    直接按扩展名逐个 stat：Path.glob 要枚举整个目录做名称匹配，
    海报目录涨到几十万文件后单次未命中查询要几十毫秒（stat 仅 0.05ms），
    详情批量拉取时每部影片都要查一次，glob 会把任务拖慢数小时。
    """
    if not idcode:
        return ""
    d = poster_dir()
    for ext in _POSTER_EXTS:
        p = d / f"{idcode}.{ext}"
        if p.is_file():
            return p.name
    return ""


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
    target = poster_dir() / name
    tmp = target.with_name(target.name + ".part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)  # 原子替换，避免读到半张图
    except OSError as exc:
        logger.warning("海报落盘失败 %s: %s", name, exc)
        tmp.unlink(missing_ok=True)
        return ""
    return name
