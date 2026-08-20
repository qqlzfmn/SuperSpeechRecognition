"""自由模式分段分发：FIFO 队列 + 分发线程 + 保序插入。纯逻辑，无框架依赖。"""
import os
import threading
import time

from .sounds import log, play_sound


class SegmentDispatcher:
    """自由模式段处理队列：段切片入队，单分发线程按 FIFO 串行 API+插入，保证顺序。"""

    def __init__(self, api, inserter, tmp_dir, min_hold_sec=0.4):
        self.api = api
        self.inserter = inserter
        self.tmp_dir = tmp_dir
        self.min_hold_sec = min_hold_sec
        self._queue = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._busy = False   # 分发线程正在处理某段
        self._done = False   # 会话结束，无更多段入队

    # ---------- 状态 ----------

    def is_busy_or_queued(self):
        with self._lock:
            return self._busy or bool(self._queue)

    def mark_done(self):
        with self._lock:
            self._done = True
            self._cv.notify_all()

    def reset(self):
        with self._lock:
            self._queue.clear()
            self._busy = False
            self._done = False

    # ---------- 入队与分发 ----------

    def enqueue(self, path):
        with self._lock:
            self._queue.append(path)
            self._cv.notify()

    def start(self):
        threading.Thread(target=self._dispatch_loop, daemon=True).start()

    def _dispatch_loop(self):
        """FIFO 顺序处理分段：API + 插入（保序）"""
        while True:
            with self._lock:
                if self._queue:
                    seg = self._queue.pop(0)
                elif self._done:
                    break
                else:
                    self._cv.wait(timeout=0.5)
                    continue
            self._busy = True
            try:
                self._process_segment(seg)
            finally:
                self._busy = False

    def _process_segment(self, path):
        """段处理：API + 哨兵过滤 + 插入（不碰主状态机，保序由 FIFO 分发保证）"""
        log("… 识别并优化中")
        try:
            text = self.api.transcribe(path, self.api.system_prompt)
            if not text:
                return
            if self.api.is_empty_reply(text):
                log("未检测到语音，已跳过")
                return
            log("✓ " + text)
            self.inserter.insert(text)
            play_sound("done")
        except Exception as e:
            play_sound("error")
            log(f"✗ 处理失败: {e}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def wait_drain(self, timeout=60.0):
        """等待队列清空（分发线程处理完毕）"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                empty = not self._queue and not self._busy
            if empty:
                return True
            time.sleep(0.2)
        return False
