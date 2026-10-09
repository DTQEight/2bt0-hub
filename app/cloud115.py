"""115 网盘操作：磁力转存离线、目录浏览、Cookie 管理。

移植自 115-project，适配本项目的 DATA_DIR 路径约定。
配置文件（加密 Cookie、默认转存目录）存于 DATA_DIR/cloud115_config.json。
"""

import json
import logging
import os
import threading
import time
from typing import Optional

import requests

from crypto_utils import encrypt, decrypt

CONFIG_FILE = os.path.join(
    os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__))),
    'cloud115_config.json')

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

logger = logging.getLogger('resource-hub.cloud115')

_cookie_cache: Optional[str] = None
_cookie_cache_ts: float = 0
_COOKIE_CACHE_TTL: int = 30

_config_lock = threading.Lock()


def _load_unlocked():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _save_unlocked(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_config():
    with _config_lock:
        return _load_unlocked()


def save_config(config):
    with _config_lock:
        _save_unlocked(config)


def update_config(mutator):
    """事务性更新配置：load → mutator(config) → save，整个过程持有锁"""
    with _config_lock:
        config = _load_unlocked()
        mutator(config)
        _save_unlocked(config)


def _request_with_retry(method, url, max_retries=3, **kwargs):
    resp = None
    for attempt in range(max_retries):
        try:
            if method == 'GET':
                resp = requests.get(url, **kwargs)
            else:
                resp = requests.post(url, **kwargs)
            if resp.status_code in (405, 429, 500, 502, 503):
                if attempt < max_retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
            return resp
        except (requests.Timeout, requests.ConnectionError):
            if attempt < max_retries - 1:
                time.sleep(1 * (attempt + 1))
                continue
            raise
    return resp


def get_cookie_string():
    global _cookie_cache, _cookie_cache_ts
    now = time.time()
    if _cookie_cache is not None and (now - _cookie_cache_ts < _COOKIE_CACHE_TTL):
        return _cookie_cache
    config = load_config()
    cookie = decrypt(config.get('cookie', ''))
    _cookie_cache = cookie
    _cookie_cache_ts = now
    return cookie


def invalidate_cookie_cache():
    global _cookie_cache, _cookie_cache_ts
    _cookie_cache = None
    _cookie_cache_ts = 0


def _get_headers():
    return {
        'User-Agent': USER_AGENT,
        'Cookie': get_cookie_string(),
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://115.com/',
        'Origin': 'https://115.com',
    }


def verify_cookie():
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'
    try:
        resp = requests.get('https://my.115.com/?ct=ajax&ac=nav',
                            headers={'User-Agent': USER_AGENT, 'Cookie': cookie}, timeout=10)
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}'
        try:
            data = resp.json()
            if data.get('state') is True and data.get('data'):
                user_name = data['data'].get('user_name', '未知')
                return True, f'验证成功 (用户: {user_name})'
            return False, 'Cookie已失效，请重新获取'
        except json.JSONDecodeError:
            return False, '响应格式错误'
    except requests.exceptions.Timeout:
        return False, '请求超时，请检查网络连接'
    except requests.exceptions.ConnectionError:
        return False, '连接失败，请检查网络'
    except Exception as e:
        return False, f'验证失败: {str(e)}'


def add_offline_task(magnet_url, save_path_id=None):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'
    if save_path_id is None:
        save_path_id, _ = get_default_save_path()
    try:
        data = {'url': magnet_url, 'wp_path_id': save_path_id}
        resp = requests.post('https://115.com/web/lixian/?ct=lixian&ac=add_task_url',
                             headers=_get_headers(), data=data, timeout=30)
        result = resp.json()
        if result.get('state') is True or result.get('state') == 1:
            return True, '离线任务添加成功'
        error_msg = result.get('error_msg') or result.get('error') or result.get('msg') or '未知错误'
        return False, f'添加失败: {error_msg}'
    except requests.Timeout:
        return False, '请求超时，请稍后重试'
    except Exception as e:
        return False, f'请求失败: {str(e)}'


def batch_add_offline_tasks(magnet_urls, save_path_id=None):
    if save_path_id is None:
        save_path_id, _ = get_default_save_path()
    results = []
    for magnet in magnet_urls:
        success, msg = add_offline_task(magnet, save_path_id)
        results.append({'magnet': magnet, 'success': success, 'message': msg})
        if success:
            time.sleep(1)
    return results


def get_task_list(page=1, page_row=100):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []
    try:
        nav_resp = requests.get('https://my.115.com/?ct=ajax&ac=nav',
                                headers=_get_headers(), timeout=10)
        nav_data = nav_resp.json()
        if nav_data.get('state') is not True:
            return False, '115 Cookie 已失效，请重新获取后再试', []
    except (json.JSONDecodeError, Exception):
        pass
    try:
        data = {'page': int(page), 'page_row': int(page_row)}
        resp = requests.post('https://115.com/web/lixian/?ct=lixian&ac=task_lists',
                             headers=_get_headers(), data=data, timeout=15)
        if resp.status_code != 200:
            snippet = resp.text[:150].replace('\n', ' ').replace('\r', '')
            return False, f'请求失败，状态码: {resp.status_code}。响应片段：{snippet}', []
        result = resp.json()
        state_ok = (
            result.get('state') is True or result.get('state') == 1
            or result.get('errcode') == 0 or result.get('errno') == 0 or result.get('code') == 0
            or (isinstance(result.get('data'), dict) and 'tasks' in result.get('data', {})))
        if state_ok:
            tasks = (result.get('result') or result.get('data') or result.get('tasks')
                     or result.get('list') or [])
            if isinstance(tasks, dict):
                tasks = tasks.get('tasks') or tasks.get('list') or tasks.get('items') or []
            return True, '获取成功', tasks if isinstance(tasks, list) else []
        err = (result.get('error_msg') or result.get('error') or result.get('errmsg')
               or result.get('msg') or result.get('message') or '获取任务列表失败')
        return False, str(err), []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def get_dir_list(cid='0'):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []
    try:
        url = (f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_utime&asc=0'
               f'&offset=0&show_dir=1&snap=0&natsort=1&format=json')
        resp = _request_with_retry('GET', url, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            url2 = (f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_ptime&asc=0'
                    f'&offset=0&show_dir=1&snap=0&natsort=1&format=json&cur=1')
            resp = _request_with_retry('GET', url2, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}', []
        data = resp.json()
        if data.get('errno') == 0 or data.get('state') is True:
            dirs = []
            for item in data.get('data', []) or []:
                if isinstance(item, dict) and item.get('cid') and item.get('n'):
                    dirs.append({'cid': str(item['cid']), 'name': item['n'],
                                 'parent_id': str(item.get('pid', '0'))})
            return True, '获取成功', dirs
        return False, f'获取目录列表失败: {data.get("error", "未知错误")}', []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []


def get_default_save_path():
    config = load_config()
    return config.get('save_path_id', '0'), config.get('save_path_name', '根目录')


def set_default_save_path(path_id, path_name=None):
    def _update(cfg):
        cfg['save_path_id'] = path_id
        if path_name:
            cfg['save_path_name'] = path_name
    update_config(_update)


def create_dir(parent_cid, dir_name):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie'
    try:
        data = {'pid': parent_cid, 'cname': dir_name}
        resp = requests.post('https://webapi.115.com/files/add',
                             headers=_get_headers(), data=data, timeout=15)
        result = resp.json()
        if result.get('errno') == 0 or result.get('state') is True:
            cid = result.get('cid', result.get('file_id', ''))
            return True, str(cid) if cid else '目录创建成功'
        return False, f'创建失败: {result.get("error", "未知错误")}'
    except Exception as e:
        return False, f'创建失败: {str(e)}'


def list_files(cid='0', offset=0, limit=100, show_dir=1):
    cookie = get_cookie_string()
    if not cookie:
        return False, '未配置115 Cookie', []
    try:
        url = (f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_utime&asc=0'
               f'&offset={offset}&show_dir={show_dir}&snap=0&natsort=1&format=json&limit={limit}')
        resp = _request_with_retry('GET', url, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            url2 = (f'https://webapi.115.com/files?aid=1&cid={cid}&o=user_ptime&asc=0'
                    f'&offset={offset}&show_dir={show_dir}&snap=0&natsort=1'
                    f'&format=json&limit={limit}&cur=1')
            resp = _request_with_retry('GET', url2, headers=_get_headers(), timeout=15)
        if resp.status_code != 200:
            return False, f'请求失败，状态码: {resp.status_code}', []
        data = resp.json()
        if data.get('errno') == 0 or data.get('state') is True:
            items = []
            for item in data.get('data', []) or []:
                if not isinstance(item, dict):
                    continue
                is_dir = item.get('cid') and not item.get('fid')
                if is_dir:
                    items.append({'type': 'dir', 'cid': str(item['cid']),
                                  'name': item.get('n', ''),
                                  'parent_id': str(item.get('pid', cid))})
                elif item.get('fid'):
                    items.append({'type': 'file', 'fid': str(item['fid']),
                                  'name': item.get('n', ''), 'size': int(item.get('s', 0)),
                                  'pickcode': item.get('pc', ''),
                                  'parent_id': str(item.get('pid', cid))})
            return True, '获取成功', items
        return False, f'获取列表失败: {data.get("error", "未知错误")}', []
    except Exception as e:
        return False, f'获取失败: {str(e)}', []
