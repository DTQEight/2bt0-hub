"""后台全量同步：把 2bt0 电影 / 电视剧种子列表全部分页抓进本地数据库。

- 一次同步一个板块（电影或电视剧），并发抓取（默认 4 线程）
- 三种运行模式（启动时自动选择，无需手动指定）：
  * full   全量抓取：从第 1 页抓到末尾，逐页记录断点
  * resume 断点续抓：上次中断（手动停止/容器重启/异常）后，从断点页继续
  * update 增量更新：全量完成后再次运行，从第 1 页重抓，
    连续 10 页全部已入库（无新资源）即提前结束，通常几十秒完成
- 末尾判定（该站 total 字段恒 400 不可信，且存在中途短页/空页）：
  * 短页（不满 20 条）不视为末尾，正常入库继续
  * 连续 2 个空页后，向 +1/+5/+20/+100 页探查，全部为空才判定到头
- 关键节点写 app.log，可在前端"日志"页实时查看
- 单页失败自动重试（api_get 内 2 次）后仍失败不中断整体，先记账；
  主循环结束后把失败页统一补抓一轮（批内失败页会被后面的成功页
  把断点"顶过去"，不补抓就永久漏页），补抓仍失败的写进结束消息
- 连续 12 页失败才终止（避免站点长时间故障导致无限循环）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from db import (clear_sync_progress, get_sync_done, get_sync_progress,
                get_stats, set_sync_done, set_sync_progress, upsert_items)
from movies import movie_detail_manager
from sources.bt0 import BASE, SECTIONS, api_get, movie_ref

logger = logging.getLogger("resource-hub.sync")

# 增量更新模式：连续 N 页全部已存在（无新资源）即判定已追平，提前结束
EARLY_STOP_PAGES = 10
# 连续 N 页请求失败（已含 api_get 内部重试）才终止同步
MAX_ERR_STREAK = 12

MODE_LABELS = {"full": "全量抓取", "resume": "断点续抓", "update": "增量更新"}


def _fetch_page(sc: int, page: int):
    """抓单页原始数据，返回 (rows, err)"""
    try:
        rows = api_get("getTList", {"sc": sc, "page": page}).get("list") or []
        return rows, None
    except Exception as exc:
        return [], str(exc)


def _really_end(sc: int, page: int) -> bool:
    """连续空页后向远处探查，全部为空才确认板块结束（防中途数据空洞）"""
    for gap in (1, 5, 20, 100):
        rows, err = _fetch_page(sc, page + gap)
        if rows or err:  # 远处有数据，或网络异常无法确认 → 不结束
            return False
    return True


def _probe_total_pages(sc: int, stop: threading.Event) -> int:
    """二分探测板块末页（约 18 个请求，用于进度百分比和 ETA 估算）。

    探测值是当下快照，站点更新后会缓慢增长；ETA 仅作估算。
    """
    def has(page: int) -> bool:
        if stop.is_set():
            return True  # 让循环尽快收敛退出
        rows, _ = _fetch_page(sc, page)
        return bool(rows)

    if not has(1):
        return 0
    lo, hi = 1, 400000
    while lo < hi and not stop.is_set():
        mid = (lo + hi + 1) // 2
        if has(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


class SyncManager:
    """单实例后台同步管理器（start / stop / status）"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples: deque = deque(maxlen=150)  # (monotonic, page) 速度采样
        self._auto_details = True  # 增量结束后是否自动跟进拉影片详情
        self.state = {
            "running": False,
            "source": "bt0",
            "section": 0,
            "page": 0,
            "fetched": 0,  # 本次同步累计抓取条数
            "message": "",
            "mode": "",  # full / resume / update
            "total_pages": 0,  # 本次探测的板块末页（0=未知）
            "speed_ppm": 0.0,  # 页/分钟
            "eta_seconds": None,
        }

    # ---- 对外接口 ----

    def start(self, section: int, start_page: int | None = None,
              workers: int = 4, mode: str | None = None,
              auto_details: bool = True) -> None:
        """启动同步。

        start_page 不传时自动选择：有断点 → 断点续抓；已全量完成 → 增量更新；
        否则 → 全量抓取。mode 可显式指定：
        - "full"   全量（有断点则自动继续）
        - "update" 增量更新（要求该板块已完成全量同步）

        auto_details：同步结束后是否自动跟进本板块的影片详情。定时任务与手动
        同步都走这条路径；传 False 可关掉（例如外层已自行安排拉详情）。
        """
        if self.state["running"]:
            raise RuntimeError("同步已在进行中，请先停止")
        if section not in SECTIONS:
            raise ValueError(f"无效板块 {section}，仅支持: {SECTIONS}")
        done = get_sync_done("bt0")
        if mode == "update" and section not in done:
            raise ValueError("该板块尚未完成全量同步，请先执行全量同步")
        if start_page is not None:
            auto_mode, page = "full", max(1, start_page)
        else:
            progress = get_sync_progress("bt0")
            if section in progress:
                auto_mode, page = "resume", progress[section] + 1
            elif section in done:
                auto_mode, page = "update", 1
            else:
                auto_mode, page = "full", 1
        mode = mode if mode in MODE_LABELS else auto_mode
        self._stop.clear()
        self._samples.clear()
        self._auto_details = auto_details
        self.state.update(
            running=True, section=section, mode=mode,
            page=page, fetched=0, message="",
            total_pages=0, speed_ppm=0.0, eta_seconds=None,
        )
        self._thread = threading.Thread(
            target=self._run, args=(section, page, workers, mode), daemon=True)
        self._thread.start()
        logger.info("同步启动：%s（板块 %d），%s，从第 %d 页开始，并发 %d",
                    SECTIONS[section], section, MODE_LABELS[mode], page, workers)

    def stop(self) -> None:
        if self.state["running"]:
            logger.info("收到停止指令，当前 %s 第 %d 页（断点已保存，可续抓）",
                        SECTIONS.get(self.state["section"], "?"), self.state["page"])
        self._stop.set()

    def status(self) -> dict:
        s = dict(self.state)
        s["section_label"] = SECTIONS.get(s["section"], "")
        s["progress"] = {str(k): v for k, v in get_sync_progress("bt0").items()}
        s["done"] = get_sync_done("bt0")
        # 速度 / ETA（至少 30 秒采样窗口才计算，避免起步阶段抖动）
        if s["running"] and len(self._samples) >= 2:
            (t0, p0), (t1, p1) = self._samples[0], self._samples[-1]
            dt = t1 - t0
            if dt >= 30 and p1 > p0:
                s["speed_ppm"] = round((p1 - p0) / (dt / 60), 1)
                if s["speed_ppm"] > 0 and s["total_pages"] > p1:
                    s["eta_seconds"] = round(
                        (s["total_pages"] - p1) / s["speed_ppm"] * 60)
        # 库统计
        stats = get_stats()
        s["db_total"] = stats["total"]
        s["by_category"] = stats["by_category"]
        s["db_size_mb"] = stats["db_size_mb"]
        s["last_seen"] = stats["last_seen"]
        return s

    # ---- 内部实现 ----

    def _run(self, section: int, start_page: int, workers: int, mode: str) -> None:
        label = SECTIONS[section]
        early_stopped = False
        natural_end = False  # 仅当确认抓到板块真正末页时为 True（防误标"已完成"）
        new_total = 0        # 本次入库的新条数（增量更新用于结算提示）
        failed_pages: list[int] = []  # 主循环中抓取失败的页，结束后补抓
        still_failed: list[int] = []  # 补抓一轮后仍失败的页（写进结束消息）
        try:
            if mode != "update":
                # 先探测板块末页（进度百分比 + ETA 用），约 18 个请求
                self.state["total_pages"] = _probe_total_pages(section, self._stop)
                if self.state["total_pages"]:
                    logger.info("%s 末页探测完成：约 %d 页（含新增会略增）",
                                label, self.state["total_pages"])
            with ThreadPoolExecutor(max_workers=workers) as ex:
                page = start_page
                known_streak = 0   # 增量模式：连续无新资源的页数
                empty_streak = 0   # 连续空页数
                err_streak = 0     # 连续失败页数
                while not self._stop.is_set():
                    # 预取一批页并发抓取，按页码顺序处理
                    futures = [ex.submit(_fetch_page, section, p)
                               for p in range(page, page + workers)]
                    batch = sorted((f.result() + (p,) for f, p in
                                    zip(futures, range(page, page + workers))),
                                   key=lambda t: t[2])
                    end = False
                    for rows, err, p in batch:
                        if end or self._stop.is_set():
                            break
                        if err:
                            err_streak += 1
                            failed_pages.append(p)
                            self.state["message"] = f"第 {p} 页失败: {err[:80]}"
                            logger.warning("%s 第 %d 页抓取失败（连续第 %d 页）: %s",
                                           label, p, err_streak, err)
                            if err_streak >= MAX_ERR_STREAK:
                                end = True
                                self.state["message"] = (
                                    f"连续 {err_streak} 页失败，同步终止（断点已保存，可稍后续抓）")
                            continue
                        err_streak = 0
                        if not rows:
                            # 空页：可能是末尾，也可能是站点数据空洞，先记账继续
                            empty_streak += 1
                            if empty_streak >= 2 and _really_end(section, p):
                                end = True
                                natural_end = True
                                break
                            continue
                        empty_streak = 0
                        new_count = self._save(section, p, rows, mode)
                        new_total += max(0, new_count)
                        # 增量模式：连续 EARLY_STOP_PAGES 页无新资源 → 已追平
                        if mode == "update":
                            if new_count > 0:
                                known_streak = 0
                            elif new_count == 0:
                                known_streak += 1
                                if known_streak >= EARLY_STOP_PAGES:
                                    end = True
                                    early_stopped = True
                                    break
                    if end or self._stop.is_set():
                        break
                    page += workers
            # 失败页补抓：批内失败页会被同批/后续成功页把断点"顶过去"，
            # 不补抓就永久漏页（断点续抓只会从更靠后的页继续）
            if failed_pages and not self._stop.is_set():
                top = self.state["page"]
                logger.info("%s 补抓失败页：%s", label, failed_pages)
                for i, p in enumerate(failed_pages):
                    if self._stop.is_set():
                        still_failed.extend(failed_pages[i:])
                        break
                    rows, err = _fetch_page(section, p)
                    if err or not rows:
                        still_failed.append(p)
                        continue
                    new_total += max(0, self._save(section, p, rows, mode))
                if mode != "update":
                    # 补抓的页码更小会把断点写回头，恢复到最高已存页
                    top = max(top, self.state["page"])
                    set_sync_progress("bt0", section, top)
                    self.state["page"] = top
                if still_failed:
                    preview = ", ".join(str(p) for p in still_failed[:8])
                    logger.warning("%s 补抓后仍失败 %d 页：%s%s", label,
                                   len(still_failed), preview,
                                   "…" if len(still_failed) > 8 else "")
        except Exception as exc:  # 兜底：任何异常都不能让线程僵死在 running 状态
            self.state["message"] = f"同步异常终止: {exc}"
            logger.exception("%s 同步异常终止", label)
        finally:
            self.state["running"] = False
            if self._stop.is_set():
                if not self.state["message"]:
                    self.state["message"] = "已手动停止（断点已保存）"
                logger.info("同步停止：%s，已完成到第 %d 页，可断点续抓",
                            label, self.state["page"])
            elif early_stopped:
                self.state["message"] = (f"已是最新：本次新增 {new_total} 条" if new_total
                                         else "已是最新，无新增资源")
                logger.info("%s 增量更新完成：本次新增 %d 条，连续 %d 页无新资源，提前结束",
                            label, new_total, EARLY_STOP_PAGES)
            elif natural_end:
                # 确认抓到板块真正末页：标记完成并清除断点，下次运行自动转增量更新
                set_sync_done("bt0", section)
                clear_sync_progress("bt0", section)
                self.state["message"] = "全量同步完成"
                logger.info("%s 全量同步完成：抓到第 %d 页，本次共 %d 条",
                            label, self.state["page"], self.state["fetched"])
            else:
                # 网络异常 / 连续失败 / 未知异常终止：保留断点，绝不标记完成
                logger.warning("%s 同步未完成即终止（断点已保留在第 %d 页）：%s",
                               label, self.state["page"], self.state["message"] or "未知原因")
            if still_failed:
                preview = ", ".join(str(p) for p in still_failed[:8]) + \
                    ("…" if len(still_failed) > 8 else "")
                self.state["message"] = ((self.state["message"] or "同步结束")
                                         + f"；{len(still_failed)} 页补抓后仍失败（{preview}）")
            # 同步结束后自动跟进本板块的影片详情（海报/年份/评分，供海报墙显示）
            # 详情是独立后台任务，这里只负责启动、不等它跑完（顶栏会接着显示详情进度）
            if self._auto_details and not self._stop.is_set():
                self._fetch_new_movie_details()

    def _fetch_new_movie_details(self) -> None:
        """同步结束后自动拉取本板块的新增影片详情。

        只处理待拉取队列（种子里出现过、movies 表还没有的影片），
        老片不会重复请求；失败只记日志，不影响同步结果。
        """
        if movie_detail_manager.state["running"]:
            logger.info("影片详情：已有任务在进行，跳过自动拉取（下次同步会继续）")
            return
        try:
            movie_detail_manager.start(self.state["section"])
        except (ValueError, RuntimeError) as exc:
            logger.info("影片详情：无需拉取（%s）", exc)

    def _save(self, sc: int, page: int, rows: list[dict], mode: str) -> int:
        """入库一页，返回本页新增条数（-1 表示入库失败，不计入增量判断）"""
        label = SECTIONS[sc]
        items = [{
            "magnet": (r.get("zlink") or "").strip(),
            "title": r.get("zname") or r.get("title") or "",
            "size": r.get("zsize") or "",
            "published_at": r.get("eztime") or "",
            "category": label,
            "detail_url": BASE + r["aurl"] if r.get("aurl") else "",
            "extra": movie_ref(r),
        } for r in rows]
        try:
            new_count = upsert_items("bt0", items)
        except Exception as exc:
            self.state["message"] = f"第 {page} 页入库失败: {exc}"
            logger.exception("%s 第 %d 页入库失败", label, page)
            return -1
        self.state["page"] = page
        self.state["fetched"] += len(rows)
        self._samples.append((time.monotonic(), page))  # 速度采样
        # 增量模式不记断点（重跑很快，且断点会干扰续抓语义）
        if mode != "update":
            set_sync_progress("bt0", sc, page)
        if page % 10 == 0 or len(rows) < 20:
            logger.info("%s 第 %d 页完成，本次已抓 %d 条%s", label, page,
                        self.state["fetched"],
                        f"，新增 {new_count}" if mode == "update" else "")
        return new_count


sync_manager = SyncManager()
