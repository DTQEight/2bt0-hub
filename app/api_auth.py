"""对外 API 的密钥鉴权（只保护"取磁力"这类会被自动化调用的接口）。

为什么需要：这个服务默认绑在 127.0.0.1，但用户会用 Windows portproxy 把 8000
转发出去供局域网/NAS 访问，而全站没有任何鉴权。一旦把「按片取磁力」开放成 API，
同网段任何人拿到地址就能批量拉走全部磁力。所以给对外 API 加一把钥匙。

密钥放数据库 settings 表（随 DATA_DIR 持久化，重建容器不丢），首次启动自动
生成并打印一次到日志。调用方带 `X-API-Key: <密钥>` 头或 `?key=<密钥>` 查询参数
（后者方便在浏览器里直接点开验证，但会留在访问日志里，正式集成建议用头）。

设计取舍：
- **不做 localhost 豁免**：容器内看到的来源 IP 是 Docker 网关（172.18.0.1），
  本机与局域网请求在应用层无法区分，按 IP 放行等于没防护。
- 查看密钥只允许私有网段（见 is_private_client），公网访问拿不到密钥本身。
"""

from __future__ import annotations

import ipaddress
import logging
import secrets

from fastapi import HTTPException, Request

from db import get_setting, set_setting

logger = logging.getLogger("resource-hub.auth")

_SETTING_KEY = "api.key"


def get_api_key() -> str:
    """读取 API 密钥；不存在则生成一个并持久化。

    生成后打印到日志（容器启动时可见一次），也通过 /api/apikey 供私有网络查看。
    """
    key = get_setting(_SETTING_KEY, "")
    if not key:
        # token_urlsafe(24) → 32 字符，熵足够，肉眼可复制
        key = secrets.token_urlsafe(24)
        set_setting(_SETTING_KEY, key)
        logger.info("已生成对外 API 密钥（请求头 X-API-Key: %s）", key)
    return key


def _client_is_private(request: Request) -> bool:
    """来源是否为私有网段/本机（用于「查看密钥」这类管理动作）。

    用 ipaddress 精确判断网段，不要用字符串前缀：`"172.2"` 这种前缀会把
    `172.217.x`（公网）、`172.203.x` 也算成内网，等于把密钥公开。
    """
    host = request.client.host if request.client else ""
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    if addr.is_loopback:
        return True
    if addr.version == 4 and addr in ipaddress.ip_network("172.16.0.0/12"):
        return True       # Docker 网关等私有网段（含 172.17/172.18）
    return addr.is_private


def require_api_key(request: Request) -> None:
    """FastAPI 依赖：校验 X-API-Key 头（或 ?key= 参数），不通过抛 401。"""
    supplied = (request.headers.get("X-API-Key")
                or request.query_params.get("key") or "").strip()
    if not supplied:
        raise HTTPException(
            status_code=401,
            detail="缺少 API 密钥：请带请求头 X-API-Key（网页「同步」页或 /api/apikey 可查看）",
        )
    # compare_digest：定长比较，避免按字节提前返回泄露前缀
    if not secrets.compare_digest(supplied, get_api_key()):
        raise HTTPException(status_code=401, detail="API 密钥不正确")


def api_key_for_admin(request: Request) -> dict:
    """查看密钥（仅供私有网络内的调用方，用于把密钥填进自己的脚本）。"""
    if not _client_is_private(request):
        raise HTTPException(status_code=403, detail="仅允许在局域网/本机查看密钥")
    return {"api_key": get_api_key()}
