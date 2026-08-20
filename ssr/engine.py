"""引擎：VoiceIME 状态机 + 编排。

逻辑层只依赖协议对象（ApiClient / TextInserter / SegmentDispatcher / vad 函数）；
run 与 _tap_cb 为 ADAPTER（Quartz CGEventTap）。
"""
import collections
import threading
import time
from pathlib import Path

from . import recorder as rec_mod
from . import vad
from .api import ApiClient
from .freemode import SegmentDispatcher
from .hotkey import (KEYCODES, MOD_FLAG_BITS, key_label, parse_cancel_key, parse_hotkey)
from .sounds import log, play_sound
from .typing import TextInserter


class VoiceIME:
    # 双击判定窗口 / 最短有效录音（秒）
    CLICK_MS = 0.4
    MIN_HOLD_SEC = 0.4

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = "idle"  # idle | recording | processing
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()   # 停止录音
        self.cancel_evt = threading.Event()  # 丢弃结果
        self.gen = 0                          # 代次，防旧线程改状态
        self.tmp_dir = Path(cfg.get("tmp_dir", "/tmp"))
        self.trigger = cfg.get("trigger", "toggle")  # hold=按住/双击 | toggle=按一下切换
        if self.trigger == "hold":
            hk = cfg["hotkey"]
            if isinstance(hk, int):
                self.hot_keycode = int(hk)  # 直接按键码，如 93
            elif isinstance(hk, str) and hk.strip().lower() in KEYCODES:
                self.hot_keycode = KEYCODES[hk.strip().lower()]
            else:
                raise ValueError(f"无法解析 hotkey: {hk!r}（数字按键码，或名称: {'/'.join(KEYCODES)}）")
            self.hot_mask = self.hot_forbidden = 0
        else:
            self.hot_keycode, self.hot_mask, self.hot_forbidden = parse_hotkey(str(cfg["hotkey"]))
        self.cancel_keycode = parse_cancel_key(cfg["cancel_key"])
        self._last_hotkey_ts = 0.0
        self._prev_keyup = 0.0        # 上次松开时间，用于双击判定
        self._dbl_block_until = 0.0   # 双击冷却：切换后短时间内不识别新双击
        self._stop_timer = None       # 松开后的延迟发送计时（给双击留窗口）
        self.free_mode = False        # 自由说话模式
        self.free_exit_evt = threading.Event()
        self._hot_down = False        # 触发键当前是否处于按下（防重复上报误判）
        self._tap = None
        self._source = None
        # 服务对象（协议注入点）
        self.api = ApiClient(cfg["base_url"], cfg["api_key"], cfg["model"],
                             cfg["system_prompt"], cfg["max_tokens"], cfg["timeout"])
        self.inserter = TextInserter(cfg.get("insert_method", "type"))
        self.dispatcher = SegmentDispatcher(self.api, self.inserter, self.tmp_dir, self.MIN_HOLD_SEC)

    # ---------- 状态 ----------

    def _set_state(self, st, gen=None):
        with self.lock:
            if gen is not None and gen != self.gen:
                return False
            self.state = st
            return True

    def _get_state(self):
        with self.lock:
            return self.state

    def _bump(self):
        with self.lock:
            self.gen += 1
            return self.gen

    # ---------- 事件 ----------

    def on_hotkey(self):
        """按键按下。单击=开始录音；双击的第二下=丢弃本段"""
        st = self._get_state()
        if self.free_mode:
            return  # 自由模式下忽略单击（仅双击退出，见 on_hotkey_release）
        if st == "idle":
            self._start_recording()
        elif st == "recording":
            # 双击的第二下按下：取消延迟发送，丢弃当前录音
            if self._stop_timer:
                self._stop_timer.cancel()
                self._stop_timer = None
            self.cancel_evt.set()
            self.stop_evt.set()
        elif st == "processing":
            self.cancel_evt.set()
            log("已取消本次输入")

    def on_hotkey_release(self):
        """按键松开。双击=切换自由说话模式；单击=延迟 0.4s 停止并发送"""
        now = time.time()
        double = (now - self._prev_keyup) < self.CLICK_MS and now > self._dbl_block_until
        self._prev_keyup = now
        if self.free_mode:
            if double:
                self._dbl_block_until = now + 0.8  # 冷却，防切换后误触
                self._exit_free_mode()
            return
        if double:
            self._dbl_block_until = now + 0.8
            if self._get_state() == "recording":
                if self._stop_timer:
                    self._stop_timer.cancel()
                    self._stop_timer = None
                self.cancel_evt.set()
                self.stop_evt.set()
            elif self._get_state() == "processing":
                self.cancel_evt.set()
            self._enter_free_mode()
            return
        if self._get_state() == "recording":
            if self._stop_timer:
                self._stop_timer.cancel()
            self._stop_timer = threading.Timer(self.CLICK_MS, self._fire_release)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _fire_release(self):
        self._stop_timer = None
        if self._get_state() == "recording":
            self.stop_evt.set()

    def _enter_free_mode(self):
        if self.free_mode:
            return
        if self.dispatcher.is_busy_or_queued():
            log("自由模式正在退出，请稍候再进入")
            return
        self.cancel_evt.clear()
        self.stop_evt.clear()
        self.dispatcher.reset()
        self.free_exit_evt.clear()
        self.free_mode = True
        gen = self._bump()
        threading.Thread(target=self._free_worker, args=(gen,), daemon=True).start()

    def _exit_free_mode(self):
        if not self.free_mode:
            return
        self.free_exit_evt.set()
        log("自由说话模式退出中…")

    def on_cancel(self):
        if self.free_mode:
            self._exit_free_mode()
            return
        st = self._get_state()
        if st == "recording":
            self.cancel_evt.set()
            self.stop_evt.set()
        elif st == "processing":
            self.cancel_evt.set()
            log("已取消本次输入")

    # ---------- 按住模式：录音 → VAD → 转写 → 插入 ----------

    def _start_recording(self):
        gen = self._bump()
        self.cancel_evt.clear()
        self.stop_evt.clear()
        if not self._set_state("recording", gen):
            return
        path = self.tmp_dir / f"voice_ime_{int(time.time() * 1000)}.wav"
        t = threading.Thread(target=self._record_worker, args=(str(path), gen), daemon=True)
        t.start()

    def _record_worker(self, path, gen):
        play_sound("start")
        log("● 开始录音…（松开即发送，Esc 取消）")
        ok, dur = rec_mod.record_to(path, self.stop_evt, self.cfg["sample_rate"],
                                    use_silence=False, silence_db=self.cfg["silence_db"],
                                    silence_sec=self.cfg["silence_sec"],
                                    max_duration=self.cfg["max_duration"])
        if ok is None:
            self._set_state("idle", gen)
            play_sound("error")
            log("✗ 无法录音（请检查 系统设置 → 隐私与安全性 → 麦克风）")
            return
        if self.cancel_evt.is_set():
            self._cleanup(path)
            self._set_state("idle", gen)
            play_sound("error")
            log("已取消录音")
            return
        if dur < self.MIN_HOLD_SEC:
            self._cleanup(path)
            self._set_state("idle", gen)
            log("录音过短，已忽略")
            return
        if vad.is_silent(path, float(self.cfg["vad_threshold"]), float(self.cfg["vad_keep_ms"])):
            self._skip_silent(path, gen)
            return
        play_sound("stop")
        self._process(path, gen)

    def _process(self, path, gen):
        if not self._set_state("processing", gen):
            return
        log("… 识别并优化中")
        try:
            text = self.api.transcribe(path, self.cfg["system_prompt"])
            if not text:
                raise RuntimeError("模型返回空文本")
            if self.cancel_evt.is_set() or self._get_state() != "processing":
                log("已取消本次输入")
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
            self._cleanup(path)
            self._set_state("idle", gen)

    # ---------- 自由模式：连续收音 + 分段 ----------

    def _free_worker(self, gen):  # ADAPTER（AVAudioRecorder 会话录音 + VAD 分段）
        """自由说话模式：单 recorder 连续收音（麦克风只获取一次），
        VAD 按静音分段，段切片入 SegmentDispatcher 并行发送，连按两下退出。"""
        play_sound("start")
        log("● 自由说话模式：连续收音，停顿自动分段发送；再连按两下退出")
        sess_path = self.tmp_dir / f"voice_ime_free_session_{int(time.time() * 1000)}.wav"
        self.dispatcher.start()
        rec = rec_mod.create_recorder(str(sess_path), float(self.cfg["sample_rate"]))
        if rec is None:
            play_sound("error")
            log("✗ 无法录音（请检查 系统设置 → 隐私与安全性 → 麦克风）")
            self._cleanup(str(sess_path))
            self.dispatcher.mark_done()
            self.free_mode = False
            self._set_state("idle", gen)
            return
        meter_db = rec_mod.calibrate_ambient(rec)
        vad_thr = max(10 ** (meter_db / 20.0) * 2.5, 0.003)
        vad_thr = min(vad_thr, 0.01)  # 上限，避免环境嘈杂时阈值过高漏掉安静语音
        log(f"… 环境标定完成（底噪 {meter_db:.1f}dB）")
        sess_start = time.time()
        seg_start = None    # 当前段起点（语音出现时记录）
        last_speech = None  # 最近一次检测到语音的时间
        noise_win = collections.deque(maxlen=100)  # 10s 噪声跟踪窗口
        noise_db = meter_db
        silence_db = max(noise_db + 5.0, -55.0)   # 静音阈值 = 噪声上沿 + 5dB
        try:
            while not self.free_exit_evt.is_set():
                time.sleep(0.1)
                rec.updateMeters()
                power = rec.averagePowerForChannel_(0)
                now = time.time() - sess_start
                if power is None:
                    continue
                # 自适应噪声跟踪：每 10s 用窗口 p25 更新噪声底（抗时变噪声）
                noise_win.append(power)
                if len(noise_win) >= 100:
                    s = sorted(noise_win)
                    noise_db = s[len(s) // 4]
                    silence_db = max(noise_db + 5.0, -55.0)
                    noise_win.clear()
                if power > silence_db:
                    if seg_start is None:
                        seg_start = now
                    last_speech = now
                elif seg_start is not None and now - last_speech >= float(self.cfg["silence_sec"]):
                    self._enqueue_segment(str(sess_path), seg_start, last_speech + 0.3, vad_thr)
                    seg_start = None
                if now >= float(self.cfg["max_duration"]):
                    log("自由说话模式达到最长时长，自动退出")
                    break
        finally:
            rec.stop()
        # 收尾：最后一段 + 标记分发结束 + 等待队列清空
        if seg_start is not None and last_speech is not None:
            self._enqueue_segment(str(sess_path), seg_start, last_speech + 0.3, vad_thr)
        self.dispatcher.mark_done()
        self._finish_free(gen, str(sess_path))

    def _enqueue_segment(self, sess_path, t0, t1, vad_thr=None):
        """切片 + VAD 复查 + 入队（FIFO）"""
        dur = t1 - t0
        if dur < self.MIN_HOLD_SEC:
            return
        slice_path = self.tmp_dir / f"voice_ime_seg_{int(time.time() * 1000)}.wav"
        if not vad.slice_wav(sess_path, t0, t1, str(slice_path), self.cfg["sample_rate"]):
            return
        if vad.is_silent(str(slice_path), vad_thr or float(self.cfg["vad_threshold"]),
                         float(self.cfg["vad_keep_ms"])):
            self._cleanup(str(slice_path))
            log("未检测到语音，已跳过")
            return
        self.dispatcher.enqueue(str(slice_path))
        log(f"… 分段完成，入队（{dur:.1f}s）")

    def _finish_free(self, gen, sess_path):
        """退出自由模式：等待队列清空（限时 60s），清理会话文件，复位状态"""
        self.dispatcher.wait_drain(60)
        self._cleanup(sess_path)
        self.free_mode = False
        self._set_state("idle", gen)
        play_sound("stop")
        log("自由说话模式已退出")

    # ---------- 事件监听 ----------

    def _tap_cb(self, proxy, etype, event, refcon):  # ADAPTER（Quartz CGEventTap）
        from Quartz import (CGEventGetIntegerValueField, CGEventGetFlags,
                            kCGKeyboardEventKeycode, kCGKeyboardEventAutorepeat)
        if self.cfg.get("log_keys"):
            try:
                code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                log(f"[key] type={etype} code={code} {key_label(code)}")
            except Exception:
                pass
        if etype == 10:  # kCGEventKeyDown（普通键）
            if CGEventGetIntegerValueField(event, kCGKeyboardEventAutorepeat):
                return event  # 系统按键重复，忽略（防止长按触发多次）
            code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            if code == self.cancel_keycode:
                self.on_cancel()
            elif code == self.hot_keycode:
                self._hot_down = True
                self.on_hotkey()
        elif etype == 11:  # kCGEventKeyUp（普通键）
            code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            if code == self.hot_keycode:
                self._hot_down = False
                self.on_hotkey_release()
        elif etype == 12:  # kCGEventFlagsChanged（修饰键按下/松开）
            code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            if code != self.hot_keycode:
                return event
            bit = MOD_FLAG_BITS.get(code)
            if bit is not None:
                down = bool(CGEventGetFlags(event) & bit)
                if down and self._hot_down:
                    return event  # 同一按键重复上报（键盘去抖/Karabiner），忽略
                self._hot_down = down
            else:
                self._hot_down = not self._hot_down  # 未知修饰键：每个事件即一次状态翻转
            if self._hot_down:
                self.on_hotkey()
            else:
                self.on_hotkey_release()
        return event

    def run(self):
        from Quartz import (CGEventTapCreate, CGEventTapEnable, CGEventTapIsEnabled,
                            kCGHIDEventTap, kCGHeadInsertEventTap,
                            CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
                            CFRunLoopGetCurrent, kCFRunLoopCommonModes,
                            kCFRunLoopDefaultMode, CFRunLoopRunInMode)
        tap = CGEventTapCreate(kCGHIDEventTap, kCGHeadInsertEventTap, 0,
                               (1 << 10) | (1 << 11) | (1 << 12),  # KeyDown|KeyUp|FlagsChanged（PyObjC 无掩码常量）
                               self._tap_cb, None)
        if tap is None or not CGEventTapIsEnabled(tap):
            print("=" * 60, flush=True)
            print("无法启用全局键盘监听。", flush=True)
            print("请到: 系统设置 → 隐私与安全性 → 辅助功能 → 勾选你的终端 App（如 Terminal / iTerm / ghostty），然后重新运行。", flush=True)
            print("=" * 60, flush=True)
            raise SystemExit(1)
        CGEventTapEnable(tap, True)
        self._tap = tap
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._source = source
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        if self.trigger == "hold":
            log(f"语音输入法已就绪 — 按住 {key_label(self.hot_keycode)} 说话，松开自动发送；连按两下 = 自由说话开关（Esc 取消），Ctrl+C 退出")
        else:
            log(f"语音输入法已就绪 — 按 {self.cfg['hotkey']} 说话（Esc 取消），Ctrl+C 退出")
        # 事件循环必须在创建 tap 的同一线程运行（RunInMode 周期返回，Ctrl+C 可中断）
        try:
            while True:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, False)
        except KeyboardInterrupt:
            log("退出")

    # ---------- 工具 ----------

    def _skip_silent(self, path, gen=None):
        """静音段处理：保留/删除文件并记录。返回是否保留文件"""
        keep = bool(self.cfg.get("keep_skipped_audio"))
        if keep:
            voiced, total = vad.wav_voiced_sec(path, float(self.cfg["vad_threshold"]))
            log(f"未检测到语音，已跳过（peak={vad.wav_peak(path)} voiced={voiced:.2f}s/{total:.1f}s，保留: {path}）")
        else:
            self._cleanup(path)
            log("未检测到语音，已跳过")
        if gen is not None:
            self._set_state("idle", gen)
        return keep

    def _cleanup(self, path):
        try:
            Path(path).unlink()
        except OSError:
            pass
