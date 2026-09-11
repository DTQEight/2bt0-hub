"""影片详情批量拉取：把种子里出现的影片逐个查站点 getVideoDetail 并写入 movies 表。

站点没有批量查详情的接口，只能按影片 id 逐个请求（实测单次约 0.3 秒），
所以用 4 线程并发、后台运行、可随时停止，进度实时上报。

前提：magnets 表已有 movie_id（全量同步时写入）；否则没有可拉取的影片。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from db import movie_stats, pending_movie_ids, upsert_movies
from sources.bt0 import fetch_video_detail

logger = logging.getLogger("resource-hub.movies")

WORKERS = 4
# 连续失败这么多次即中止（通常是站点异常或网络中断，继续跑没有意义）
MAX_ERR_STREAK = 30
# 攒够这么多条再写一次库（太少会让写入事务成为瓶颈）
BATCH_SIZE = 50
# 速度/ETA 至少累计这么多秒才计算，避免起步阶段抖动
MIN_WINDOW = 30


class MovieDetailManager:
    """单实例后台任务管理器（start / stop / status）"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._samples: deque = deque(maxlen=400)  # (monotonic, done) 速度采样
        self.state = {
            "running": False,
            "done": 0,        # 本次已处理（含失败）
            "total": 0,       # 本次待处理总数
            "failed": 0,
            "message": "",
            "speed": 0.0,     # 部/分钟
            "eta_seconds": None,
        }

    # ---- 对外接口 ----

    def start(self) -> None:
        if self.state["running"]:
            raise RuntimeError("影片详情拉取已在进行中，请先停止")
        pending = pending_movie_ids()
        if not pending:
            if movie_stats()["wanted"] == 0:
                raise ValueError("库里还没有影片 id，请先跑一次全量同步")
            raise ValueError("影片详情都已入库，无需拉取")
        self._stop.clear()
        self._samples.clear()
        self.state.update(running=True, done=0, total=len(pending), failed=0,
                          message="", speed=0.0, eta_seconds=None)
        self._thread = threading.Thread(target=self._run, args=(pending,), daemon=True)
        self._thread.start()
        logger.info("影片详情拉取启动：待拉取 %d 部，并发 %d", len(pending), WORKERS)

    def stop(self) -> None:
        if self.state["running"]:
            logger.info("收到停止指令，影片详情拉取已完成 %d/%d 部",
                        self.state["done"], self.state["total"])
        self._stop.set()

    def status(self) -> dict:
        s = dict(self.state)
        s.update(movie_stats())
        if s["running"] and len(self._samples) >= 2:
            (t0, d0), (t1, d1) = self._samples[0], self._samples[-1]
            dt = t1 - t0
            if dt >= MIN_WINDOW and d1 > d0:
                s["speed"] = round((d1 - d0) / (dt / 60), 1)
                if s["speed"] > 0 and s["total"] > d1:
                    s["eta_seconds"] = round((s["total"] - d1) / s["speed"] * 60)
        return s

    # ---- 内部实现 ----

    @staticmethod
    def _fetch_one(idcode: str) -> dict | None:
        """抓一部影片详情；失败返回 None（只记告警，不中断整体）"""
        try:
            return fetch_video_detail(idcode)
        except Exception as exc:
            logger.warning("影片 %s 详情拉取失败: %s", idcode, exc)
            return None

    def _run(self, pending: list[str]) -> None:
        err_streak = 0
        failed_ids: list[str] = []  # 失败的影片 id，跑完统一补抓一轮
        batch: list[dict] = []
        aborted = ""
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as ex:
                for i in range(0, len(pending), WORKERS):
                    if self._stop.is_set():
                        break
                    futures = [ex.submit(self._fetch_one, m)
                               for m in pending[i:i + WORKERS]]
                    for mid, fut in zip(pending[i:i + WORKERS], futures):
                        self.state["done"] += 1
                        res = fut.result()
                        if res is None:
                            err_streak += 1
                            self.state["failed"] += 1
                            failed_ids.append(mid)
                        else:
                            err_streak = 0
                            batch.append(res)
                    if len(batch) >= BATCH_SIZE:
                        upsert_movies(batch)
                        batch.clear()
                    self._samples.append((time.monotonic(), self.state["done"]))
                    if self.state["done"] % 1000 < WORKERS:
                        logger.info("影片详情已拉取 %d/%d 部（失败 %d）",
                                    self.state["done"], self.state["total"],
                                    self.state["failed"])
                    if err_streak >= MAX_ERR_STREAK:
                        aborted = f"连续失败 {err_streak} 次，已中止"
                        logger.warning("影片详情拉取连续失败 %d 次，中止（已完成 %d 部）",
                                       err_streak, self.state["done"])
                        break
            # 正常跑完后补抓一轮失败影片（站点偶发鉴权/限流抖动，隔几秒重试通常能过）
            if not aborted and not self._stop.is_set() and failed_ids:
                time.sleep(3)
                logger.info("补抓失败影片 %d 部", len(failed_ids))
                recovered = 0
                for mid in failed_ids:
                    if self._stop.is_set():
                        break
                    res = self._fetch_one(mid)
                    if res is not None:
                        recovered += 1
                        self.state["failed"] -= 1
                        batch.append(res)
                if recovered:
                    logger.info("补抓成功 %d/%d 部，仍失败 %d 部",
                                recovered, len(failed_ids), self.state["failed"])
        except Exception as exc:  # 兜底：任何异常都不能让线程僵死在 running 状态
            aborted = f"异常终止: {exc}"
            logger.exception("影片详情拉取异常终止")
        finally:
            if batch:
                try:
                    upsert_movies(batch)
                except Exception:
                    logger.exception("影片详情收尾入库失败")
            self.state["running"] = False
            if self._stop.is_set():
                self.state["message"] = (self.state["message"]
                                         or f"已手动停止（本次完成 {self.state['done']} 部）")
                logger.info("影片详情拉取已停止：本次完成 %d/%d 部",
                            self.state["done"], self.state["total"])
            elif aborted:
                self.state["message"] = f"{aborted}（本次完成 {self.state['done']} 部）"
            else:
                self.state["message"] = (f"拉取完成：共 {self.state['done']} 部"
                                         f"（失败 {self.state['failed']}）")
                logger.info("影片详情拉取完成：共 %d 部，失败 %d 部",
                            self.state["done"], self.state["failed"])


movie_detail_manager = MovieDetailManager()
