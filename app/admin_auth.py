"""后台管理登录：账号密码 + 会话 Cookie。

为什么单独加一道登录：
    同步 / 日志 / 115 转存都是"能动手"的操作（启停抓取、看运行日志、把资源
    转存到自己的网盘）。原来的 X-API-Key 挡不住它们——网页要读列表接口就必须
    先拿到这把钥匙，等于所有能打开网页的人都有。

做法：
- 账号密码存 settings（admin.user / admin.salt / admin.hash），PBKDF2-SHA256。
- 首次启动创建账号：用户名取环境变量 ADMIN_USER（默认 admin），密码取
  ADMIN_PASSWORD；没设就随机生成并打印到日志（docker logs 2bt0 可见），
  登录后可在后台改密码。
- 登录成功发随机 token，放 HttpOnly Cookie（admin_token，12 小时）。
  token 只存在内存里：容器重启后要重新登录，顺带免去了过期记录清理。
- 受保护路径见 ADMIN_PREFIXES，这些路径只认 Cookie，不认 API 密钥。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time

from fastapi import Request, Response

import db

logger = logging.getLogger("resource-hub.admin")

COOKIE_NAME = "admin_token"
SESSION_TTL = 12 * 3600  # 12 小时
PBKDF2_ROUNDS = 200_000

# 只有登录后才能访问的接口前缀。注意 /api/apikey 不在此列：
# 网页读列表接口要先用它换密钥，放进来会导致登录页自己打不开列表。
ADMIN_PREFIXES = (
    "/api/admin", "/api/sync", "/api/movies", "/api/logs", "/api/schedule",
    "/api/bt0cookie", "/api/bt0gate", "/api/wecom", "/api/115",
)
# 登录前后都必须能访问的接口（否则没法登录）
PUBLIC_ADMIN_PATHS = {"/api/admin/login", "/api/admin/session"}

_SESSIONS: dict[str, float] = {}  # token → 过期时间戳
_LOCK = threading.Lock()


def is_admin_path(path: str) -> bool:
    """True = 该接口需要后台登录（登录接口本身除外）。"""
    if path in PUBLIC_ADMIN_PATHS:
        return False
    return any(path.startswith(p) for p in ADMIN_PREFIXES)


# ---- 账号密码 ----

def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               bytes.fromhex(salt), PBKDF2_ROUNDS).hex()


def get_username() -> str:
    return db.get_setting("admin.user", "admin")


def ensure_admin() -> None:
    """首次启动创建后台账号（已有则不动）。"""
    if db.get_setting("admin.hash", ""):
        return
    user = (os.getenv("ADMIN_USER") or "admin").strip() or "admin"
    password = (os.getenv("ADMIN_PASSWORD") or "").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(9)  # 约 12 个字符
    salt = secrets.token_hex(16)
    db.set_setting("admin.user", user)
    db.set_setting("admin.salt", salt)
    db.set_setting("admin.hash", _hash(password, salt))
    if generated:
        logger.warning("已生成后台管理账号：%s / %s（可在后台「修改密码」里改）",
                       user, password)
    else:
        logger.info("已按环境变量创建后台管理账号：%s", user)


def verify_login(user: str, password: str) -> tuple[bool, str]:
    """校验账号密码。

    账号不对也照常算一遍哈希再返回，避免"账号错"比"密码错"快，泄露账号是否存在。
    """
    saved_user = get_username()
    user_ok = secrets.compare_digest(user, saved_user)
    salt = db.get_setting("admin.salt", "")
    saved_hash = db.get_setting("admin.hash", "")
    if not salt or not saved_hash:
        return False, "后台账号未初始化，请重启服务"
    pwd_ok = secrets.compare_digest(_hash(password, salt), saved_hash)
    if not (user_ok and pwd_ok):
        return False, "账号或密码不正确"
    return True, "ok"


def change_password(old: str, new: str) -> tuple[bool, str]:
    ok, _ = verify_login(get_username(), old)
    if not ok:
        return False, "原密码不正确"
    if len(new) < 6:
        return False, "新密码至少 6 位"
    salt = secrets.token_hex(16)
    db.set_setting("admin.salt", salt)
    db.set_setting("admin.hash", _hash(new, salt))
    logger.info("后台管理密码已更新")
    return True, "密码已更新"


# ---- 会话 ----

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        # 顺手清掉过期 token，长期运行也不会无界增长
        for t in [t for t, exp in _SESSIONS.items() if exp < now]:
            _SESSIONS.pop(t, None)
        _SESSIONS[token] = now + SESSION_TTL
    return token


def _valid(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    with _LOCK:
        exp = _SESSIONS.get(token)
        if exp is None:
            return False
        if exp < now:
            _SESSIONS.pop(token, None)
            return False
    return True


def check_session(request: Request) -> bool:
    return _valid(request.cookies.get(COOKIE_NAME, ""))


def drop_session(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token or "", None)


def attach_cookie(response: Response, token: str) -> None:
    # 公网走 Cloudflare 是 https、局域网是 http，所以不能加 secure
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
