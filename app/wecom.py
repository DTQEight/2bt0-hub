"""企业微信推送 / 回调（移植自 115transfer 的 wechat_work.py）。

配置存在 settings 表（wecom.*），不再单独维护 JSON 文件：
    wecom.corpid      企业 ID
    wecom.corpsecret  应用 Secret（AES 加密存储）
    wecom.agentid     应用 AgentId
    wecom.token       接收消息 Token
    wecom.aeskey      接收消息 EncodingAESKey（43 位）
    wecom.touser      默认接收人（默认 @all，多个用 | 分隔）
    wecom.callback_url 公网回调地址（仅用于页面展示/复制，服务端不依赖它）

用途：2bt0 同步撞图形验证码时，把验证码图片推到企业微信；人工在企微里
直接回复数字，/wechat/callback 收到消息后校验并自动续抓（见 main.py）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import random
import string
import struct
import threading
import time

import requests
from Crypto.Cipher import AES

import db
from crypto_utils import decrypt, encrypt

logger = logging.getLogger("resource-hub.wecom")

API = "https://qyapi.weixin.qq.com/cgi-bin"
_FIELDS = ("corpid", "corpsecret", "agentid", "token", "aeskey", "touser",
           "callback_url")
# access_token 失效的错误码：清缓存后重试一次
_TOKEN_ERR = (40014, 42001, 41001)

_token_lock = threading.Lock()
_token_cache = {"token": "", "expires": 0.0}


# ---- 配置读写 ----

def load_config() -> dict:
    cfg = {k: db.get_setting("wecom." + k, "") for k in _FIELDS}
    cfg["corpsecret"] = decrypt(cfg["corpsecret"])
    cfg["touser"] = cfg["touser"] or "@all"
    return cfg


def save_config(values: dict) -> dict:
    """保存非空的配置项。secret 类字段留空表示「保持原值不变」。

    corpsecret 用 crypto_utils 加密后入库（与 115 Cookie 一致）。
    """
    for key in _FIELDS:
        val = str(values.get(key) or "").strip()
        if key == "callback_url":
            db.set_setting("wecom.callback_url", val)  # 允许清空
            continue
        if not val:
            continue
        if key == "corpsecret":
            val = encrypt(val)
        db.set_setting("wecom." + key, val)
    with _token_lock:
        _token_cache.update(token="", expires=0.0)  # 配置变了，旧 token 作废
    return load_config()


def is_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return bool(cfg["corpid"] and cfg["corpsecret"] and cfg["agentid"])


# ---- 基础推送 ----

def get_access_token(force: bool = False) -> tuple[str | None, str]:
    """获取 access_token（内存缓存，提前 5 分钟过期）"""
    cfg = load_config()
    corpid, secret = cfg["corpid"], cfg["corpsecret"]
    if not corpid or not secret:
        return None, "未配置企业微信（企业 ID / Secret）"

    with _token_lock:
        if not force and _token_cache["token"] and _token_cache["expires"] > time.time():
            return _token_cache["token"], "ok"
        try:
            data = requests.get(f"{API}/gettoken",
                                params={"corpid": corpid, "corpsecret": secret},
                                timeout=10).json()
        except Exception as exc:
            return None, f"获取 access_token 失败: {exc}"
        if data.get("errcode", 0) != 0 or not data.get("access_token"):
            return None, data.get("errmsg") or "获取 access_token 失败"
        _token_cache["token"] = data["access_token"]
        _token_cache["expires"] = time.time() + data.get("expires_in", 7200) - 300
        return _token_cache["token"], "ok"


def _agentid(cfg: dict) -> int | None:
    try:
        return int(cfg.get("agentid") or 0) or None
    except (TypeError, ValueError):
        return None


def _send(payload_builder, cfg: dict) -> tuple[bool, str]:
    """发送消息：agentid / touser 由这里补全，token 失效时自动刷新重试一次。"""
    agentid = _agentid(cfg)
    if not agentid:
        return False, "AgentId 配置无效（必须为整数）"
    payload = payload_builder(agentid)

    for attempt in (0, 1):
        token, err = get_access_token(force=attempt == 1)
        if not token:
            return False, err
        try:
            data = requests.post(f"{API}/message/send",
                                 params={"access_token": token},
                                 json=payload, timeout=15).json()
        except Exception as exc:
            return False, f"请求企业微信失败: {exc}"
        if data.get("errcode") == 0:
            return True, "发送成功"
        if data.get("errcode") in _TOKEN_ERR and attempt == 0:
            continue
        return False, data.get("errmsg") or f"发送失败（errcode={data.get('errcode')}）"
    return False, "发送失败"


def send_text(content: str, to_user: str = "") -> tuple[bool, str]:
    cfg = load_config()
    if not is_configured(cfg):
        return False, "未配置企业微信"
    to = (to_user or cfg["touser"] or "@all").strip()
    text = truncate_reply(content or "")
    return _send(lambda aid: {
        "touser": to, "msgtype": "text", "agentid": aid, "text": {"content": text},
    }, cfg)


_MIME_EXT = {
    "image/png": ("captcha.png", "image/png"),
    "image/jpeg": ("captcha.jpg", "image/jpeg"),
    "image/webp": ("captcha.webp", "image/webp"),
    "image/avif": ("captcha.avif", "image/avif"),
}


def _upload(media: bytes, filename: str, mime: str,
            kind: str) -> tuple[str, str]:
    """上传临时素材，返回 (media_id, 错误文案)"""
    token, err = get_access_token()
    if not token:
        return "", err
    try:
        data = requests.post(
            f"{API}/media/upload",
            params={"access_token": token, "type": kind},
            files={"media": (filename, media, mime)},
            timeout=20).json()
    except Exception as exc:
        return "", f"上传素材失败: {exc}"
    if data.get("errcode", 0) != 0 or not data.get("media_id"):
        return "", data.get("errmsg") or "上传素材失败"
    return data["media_id"], ""


def send_image(image: bytes, to_user: str = "",
               filename: str = "captcha.png",
               mime: str = "image/png") -> tuple[bool, str]:
    """以图片消息发送（会被企微按静态图处理，动图请用 send_file）。"""
    cfg = load_config()
    if not is_configured(cfg):
        return False, "未配置企业微信"
    media_id, err = _upload(image, filename, mime, "image")
    if not media_id:
        return False, err
    to = (to_user or cfg["touser"] or "@all").strip()
    return _send(lambda aid: {
        "touser": to, "msgtype": "image", "agentid": aid,
        "image": {"media_id": media_id},
    }, cfg)


def send_file(content: bytes, filename: str, to_user: str = "",
              mime: str = "application/octet-stream") -> tuple[bool, str]:
    """按原文件发送（企业微信图片消息会把 GIF 压成静态首帧，动图走这里）。"""
    cfg = load_config()
    if not is_configured(cfg):
        return False, "未配置企业微信"
    media_id, err = _upload(content, filename, mime, "file")
    if not media_id:
        return False, err
    to = (to_user or cfg["touser"] or "@all").strip()
    return _send(lambda aid: {
        "touser": to, "msgtype": "file", "agentid": aid,
        "file": {"media_id": media_id},
    }, cfg)


def notify_captcha(section_label: str, page: int, captcha: dict,
                   to_user: str = "") -> tuple[bool, str]:
    """把图形验证码推到企业微信。

    站点验证码是 GIF 动图（字符逐帧出现），走图片消息只会显示静态首帧，
    导致人工无法识别，所以 GIF 原样作为文件发送，其它格式仍用图片消息。
    """
    if not is_configured():
        return False, "未配置企业微信"

    img = captcha.get("img") or ""
    if not img.startswith("data:") or "," not in img:
        return False, "验证码图片为空"
    header, encoded = img.split(",", 1)
    mime = header[5:header.index(";")] if header.startswith("data:") and ";" in header \
        else "image/png"
    try:
        raw = base64.b64decode(encoded)
    except Exception as exc:
        return False, f"验证码图片解码失败: {exc}"

    length = captcha.get("length") or 0
    hint = f"（{length} 位字符）" if length else ""
    is_gif = mime == "image/gif"
    view_hint = ("验证码是 GIF 动图，已按原文件发送（captcha.gif），"
                 "请点开文件看完整动画后再回复。"
                 if is_gif else "请查看下方图片后回复验证码。")
    text = (f"【2bt0 同步】{section_label or '同步'}第 {page} 页匿名查看额度已用完。\n"
            f"{view_hint}\n"
            f"直接回复图中字符{hint}即可，服务端会自动验证并继续同步。\n"
            "验证码有效期很短，过期会自动换一张重新推送。")
    ok_text, msg_text = send_text(text, to_user=to_user)
    if not ok_text:
        logger.warning("企业微信验证码说明发送失败：%s", msg_text)

    if is_gif:
        ok_media, msg_media = send_file(raw, "captcha.gif", to_user, mime="image/gif")
    else:
        filename, send_mime = _MIME_EXT.get(mime, ("captcha.png", "image/png"))
        ok_media, msg_media = send_image(raw, to_user, filename=filename, mime=send_mime)
    if not ok_media:
        return False, msg_media
    return True, "验证码已推送到企业微信（原文件）" if is_gif else "验证码已推送到企业微信"


# ---- 自定义菜单 ----

# 一级菜单最多 3 个、标题不超过 16 字节（中文约 5 字），所以用「同步/状态/帮助」
DEFAULT_MENU = {
    "button": [
        {"name": "同步", "sub_button": [
            {"type": "click", "name": "增量·电影", "key": "sync_update_1"},
            {"type": "click", "name": "增量·电视剧", "key": "sync_update_2"},
            {"type": "click", "name": "停止同步", "key": "sync_stop"},
        ]},
        {"name": "状态", "sub_button": [
            {"type": "click", "name": "同步状态", "key": "sync_status"},
            {"type": "click", "name": "库存统计", "key": "stats"},
            {"type": "click", "name": "推送验证码", "key": "captcha"},
        ]},
        {"name": "帮助", "sub_button": [
            {"type": "click", "name": "搜索片名", "key": "search_hint"},
            {"type": "click", "name": "使用说明", "key": "help"},
        ]},
    ]
}


def create_menu(menu: dict | None = None) -> tuple[bool, str]:
    """创建应用自定义菜单（agentid 自动补全；菜单约 1 分钟内生效）"""
    cfg = load_config()
    agentid = _agentid(cfg)
    if not agentid:
        return False, "AgentId 配置无效（必须为整数）"
    payload = menu or DEFAULT_MENU
    for attempt in (0, 1):
        token, err = get_access_token(force=attempt == 1)
        if not token:
            return False, err
        try:
            data = requests.post(
                f"{API}/menu/create",
                params={"access_token": token, "agentid": agentid},
                json=payload, timeout=15).json()
        except Exception as exc:
            return False, f"请求企业微信失败: {exc}"
        if data.get("errcode") == 0:
            return True, "菜单已创建（约 1 分钟内生效）"
        if data.get("errcode") in _TOKEN_ERR and attempt == 0:
            continue
        return False, data.get("errmsg") or f"创建菜单失败（errcode={data.get('errcode')}）"
    return False, "创建菜单失败"



# ---- 回调加解密（与 115transfer 的 WeChatCrypto 一致）----

class WeChatCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        # encoding_aes_key 必须为 43 位 Base64 字符串，解码后 32 字节
        if not encoding_aes_key or len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey 长度必须为 43 位")
        self.key = base64.b64decode(encoding_aes_key + "=")

    def _decrypt(self, encrypted: str) -> tuple[str, str]:
        iv = self.key[:16]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(base64.b64decode(encrypted))
        if not decrypted:
            raise ValueError("解密结果为空")
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f"PKCS7 填充长度非法: {pad_len}")
        if decrypted[-pad_len:] != bytes([pad_len] * pad_len):
            raise ValueError("PKCS7 填充字节不一致")
        decrypted = decrypted[:-pad_len]
        if len(decrypted) < 20:
            raise ValueError("解密内容过短")
        content_len = struct.unpack(">I", decrypted[16:20])[0]
        if 20 + content_len > len(decrypted):
            raise ValueError("内容长度字段非法")
        content = decrypted[20:20 + content_len].decode("utf-8")
        from_id = decrypted[20 + content_len:].decode("utf-8")
        return content, from_id

    def _encrypt(self, reply_msg: str, from_user: str) -> str:
        msg = reply_msg.encode("utf-8")
        sender = from_user.encode("utf-8")
        random_str = "".join(
            random.choices(string.ascii_letters + string.digits, k=16)).encode("utf-8")
        text = random_str + struct.pack(">I", len(msg)) + msg + sender
        block_size = 32
        pad_len = block_size - (len(text) % block_size)
        text += bytes([pad_len] * pad_len)
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(text)).decode("utf-8")

    def verify_signature(self, signature: str, timestamp: str, nonce: str,
                         echostr: str | None = None) -> bool:
        params = [self.token, timestamp, nonce]
        if echostr:
            params.append(echostr)
        params.sort()
        expected = hashlib.sha1("".join(params).encode("utf-8")).hexdigest()
        return expected == signature

    def decrypt_message(self, encrypted_msg: str) -> tuple[str, str]:
        return self._decrypt(encrypted_msg)

    def encrypt_message(self, reply_msg: str, from_user: str) -> str:
        return self._encrypt(reply_msg, from_user)


def parse_message(xml_content) -> dict | None:
    """解析企业微信消息 XML（空标签的 text 为 None 时统一成空串）"""
    import xml.etree.ElementTree as ET
    try:
        if isinstance(xml_content, bytes):
            xml_content = xml_content.decode("utf-8", errors="replace")
        root = ET.fromstring(xml_content)
        return {child.tag: child.text or "" for child in root}
    except Exception:
        return None


def _xml_escape_cdata(text: str) -> str:
    """转义 CDATA 内无法表示的 ]]>，防注入"""
    return str(text or "").replace("]]>", "]]]]><![CDATA[>")


def build_reply_xml(to_user: str, from_user: str, content: str,
                    crypto: WeChatCrypto | None = None) -> str:
    timestamp = str(int(time.time()))
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    reply_msg = f"""<xml>
<ToUserName><![CDATA[{_xml_escape_cdata(to_user)}]]></ToUserName>
<FromUserName><![CDATA[{_xml_escape_cdata(from_user)}]]></FromUserName>
<CreateTime>{timestamp}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{_xml_escape_cdata(content)}]]></Content>
</xml>"""
    if not crypto:
        return reply_msg
    encrypted = crypto.encrypt_message(reply_msg, crypto.corp_id)
    msg_signature = hashlib.sha1(
        "".join(sorted([crypto.token, timestamp, nonce, encrypted])).encode("utf-8")
    ).hexdigest()
    return f"""<xml>
<Encrypt><![CDATA[{encrypted}]]></Encrypt>
<MsgSignature><![CDATA[{msg_signature}]]></MsgSignature>
<TimeStamp>{timestamp}</TimeStamp>
<Nonce><![CDATA[{nonce}]]></Nonce>
</xml>"""


def truncate_reply(text: str, max_bytes: int = 2000) -> str:
    """企业微信文本消息上限 2048 字节，超出按 UTF-8 边界截断。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes - 3]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore") + "..."
