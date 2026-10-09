"""115 转存历史记录模块

使用 JSON 文件持久化转存历史（DATA_DIR/transfer_history.json），支持：
- 记录单条/批量转存结果
- 查询最近记录
- 统计今日/累计成功失败数
- 自动清理90天前的记录
"""
import json
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

_HISTORY_FILE: Optional[str] = None
_history_lock: threading.Lock = threading.Lock()
_RETENTION_DAYS: int = 90
_MAX_RECORDS: int = 5000


def _get_history_file() -> str:
    global _HISTORY_FILE
    if _HISTORY_FILE is None:
        data_dir = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
        _HISTORY_FILE = os.path.join(data_dir, 'transfer_history.json')
    return _HISTORY_FILE


_BJ_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    return datetime.now(_BJ_TZ).isoformat()


def _load() -> List[Dict[str, Any]]:
    path = _get_history_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    path = _get_history_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _cleanup(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not records:
        return records
    cutoff = datetime.now(_BJ_TZ) - timedelta(days=_RETENTION_DAYS)
    kept: List[Dict[str, Any]] = []
    for r in records:
        ts = r.get('time', '')
        try:
            dt = datetime.fromisoformat(ts)
            if dt >= cutoff:
                kept.append(r)
        except (ValueError, TypeError):
            kept.append(r)
    if len(kept) > _MAX_RECORDS:
        kept.sort(key=lambda x: x.get('time', ''), reverse=True)
        kept = kept[:_MAX_RECORDS]
    return kept


def add_record(info_hash: str, movie_title: str, magnet: str,
               success: bool, message: str, source: str = 'single') -> None:
    """添加一条转存历史记录"""
    record = {
        'time': _now_iso(),
        'info_hash': info_hash or '',
        'movie_title': movie_title or '',
        'magnet': (magnet or '')[:50],
        'success': bool(success),
        'message': message or '',
        'source': source,
    }
    with _history_lock:
        records = _load()
        records.append(record)
        records = _cleanup(records)
        _save(records)


def add_batch_records(results: List[Dict[str, Any]],
                      movie_map: Optional[Dict[str, Dict[str, Any]]] = None,
                      source: str = 'batch') -> None:
    """批量添加转存历史记录

    Args:
        results: batch_add_offline_tasks 返回的结果列表
        movie_map: magnet -> {info_hash, movie_title} 映射
    """
    if not results:
        return
    if movie_map is None:
        movie_map = {}
    now = _now_iso()
    new_records: List[Dict[str, Any]] = []
    for r in results:
        magnet = r.get('magnet', '')
        info = movie_map.get(magnet, {})
        new_records.append({
            'time': now,
            'info_hash': info.get('info_hash', ''),
            'movie_title': info.get('movie_title', ''),
            'magnet': magnet[:50] if magnet else '',
            'success': bool(r.get('success', False)),
            'message': r.get('message', ''),
            'source': source,
        })
    with _history_lock:
        records = _load()
        records.extend(new_records)
        records = _cleanup(records)
        _save(records)


def get_recent(limit: int = 10) -> List[Dict[str, Any]]:
    with _history_lock:
        records = _load()
    records.sort(key=lambda x: x.get('time', ''), reverse=True)
    return records[:limit]


def get_statistics() -> Dict[str, Any]:
    with _history_lock:
        records = _load()
    today_str = datetime.now(_BJ_TZ).strftime('%Y-%m-%d')
    today_success = today_fail = total_success = total_fail = 0
    for r in records:
        is_ok = r.get('success', False)
        if is_ok:
            total_success += 1
        else:
            total_fail += 1
        if r.get('time', '').startswith(today_str):
            if is_ok:
                today_success += 1
            else:
                today_fail += 1
    return {'today_success': today_success, 'today_fail': today_fail,
            'total_success': total_success, 'total_fail': total_fail}
