#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 语音输入法（无 GUI，全局热键触发）
================================================
录音 → 多模态大模型（音频理解）转写 + 口语优化 → 自动输入到当前应用

用法:
  .venv/bin/python voice_ime.py                 # 常驻运行，监听全局热键
  .venv/bin/python voice_ime.py --test          # 录 3 秒 → API → 打印结果（不插入）
  .venv/bin/python voice_ime.py --test-file x.wav  # 用已有音频文件测 API 链路
  .venv/bin/python voice_ime.py --keys          # 按键码拾取器（配置 hotkey 用）

热键（可在 config.json 修改，hotkey 支持数字按键码或名称）:
  trigger: "hold"   键(93)：按住说话，松开自动识别输入；
                    连按两下 = 开关「自由说话」模式（连续听写，停顿自动分段发送，再连按两下退出）
  trigger: "toggle" 按一下开始录音，再按一下停止发送
  Esc              取消（丢弃录音/退出自由说话模式/丢弃待插入结果）
  用 --keys 可查看任意按键的 keycode

依赖: pyobjc-framework-AVFoundation / Quartz / CoreAudio / Cocoa（见 requirements.txt）
权限: 麦克风 + 辅助功能（系统设置 → 隐私与安全性），授予你的终端 App。
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------- 配置 ----------------

DEFAULTS = {
    "base_url": "https://www.dmxapi.cn/v1",
    "api_key": "",
    "model": "mimo-v2.5",
    "hotkey": "cmd+shift+space",
    "cancel_key": "esc",
    "system_prompt": (
        "你是 macOS 语音输入助手。用户发来一段语音，请把它转写成通顺的书面中文，并优化口语："
        "1) 去掉“嗯、啊、呃、那个、就是、然后”等语气词和口头禅；"
        "2) 修正口误、重复和语序混乱；"
        "3) 补全标点，适当分段；"
        "4) 保留原意与事实信息，术语、人名、数字、英文尽量原样保留；"
        "5) 直接输出整理后的文本，不要任何解释、前缀、引号或客套话；"
        "6) 如果音频中没有清晰的语音内容（静音或纯环境音），只回复两个字：无语音。"
    ),
    "sample_rate": 16000,
    "silence_db": -45.0,      # 低于该音量视为静音（dB，0 最大 -160 最小）
    "silence_sec": 1.5,       # 持续静音多久自动停止
    "max_duration": 60,       # 最长录音秒数
    "insert_method": "type",  # type=模拟键盘输入 | paste=剪贴板粘贴
    "max_tokens": 1000,
    "timeout": 240,
    "log_keys": False,            # true = 在日志里打印每次按键事件（排错用）
    "keep_skipped_audio": False,  # true = 保留被判为静音/无语音的录音文件并记录峰值（诊断用）
}

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

KEYCODES = {
    "space": 49, "esc": 53, "return": 36, "tab": 48,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79,
    # 修饰键（用于按住说话模式）
    "right_cmd": 54, "left_cmd": 55, "right_shift": 60, "left_shift": 56,
    "right_alt": 61, "left_alt": 58, "right_ctrl": 62, "left_ctrl": 59,
}
MODIFIER_MASKS = {
    "cmd": 0x100000, "shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "option": 0x80000,
}
FORBIDDEN_MODS = ["ctrl", "alt", "option"]  # 默认排除，避免误触

# 修饰键 keycode → 对应事件标志位（flagsChanged 事件用标志位判断按下/松开）
MOD_FLAG_BITS = {
    54: 0x100000, 55: 0x100000,   # 右⌘ / 左⌘
    60: 0x20000, 56: 0x20000,     # 右⇧ / 左⇧
    61: 0x80000, 58: 0x80000,     # 右⌥ / 左⌥
    62: 0x40000, 59: 0x40000,     # 右⌃ / 左⌃
    57: 0x10000,                  # Caps Lock
    63: 0x800000,                 # Fn
}
MOD_NAMES = {
    54: "右⌘", 55: "左⌘", 56: "左⇧", 60: "右⇧",
    58: "左⌥", 61: "右⌥", 59: "左⌃", 62: "右⌃",
    57: "CapsLock", 63: "Fn", 53: "Esc", 49: "Space",
    36: "Return", 48: "Tab",
}


def key_label(code):
    return MOD_NAMES.get(int(code), f"键({code})")

SOUNDS = {"start": "Glass", "stop": "Tink", "done": "Ping", "error": "Basso"}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[config] 读取 config.json 失败: {e}，使用默认配置", flush=True)
    if not cfg["api_key"]:
        print("[config] 错误: config.json 中缺少 api_key", file=sys.stderr)
        sys.exit(1)
    return cfg


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def play_sound(name):
    try:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{SOUNDS[name]}.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------- 热键解析 ----------------

def parse_hotkey(spec):
    """'cmd+shift+space' -> (keycode, required_mask, forbidden_mask)"""
    parts = [p.strip().lower() for p in spec.split("+")]
    key = parts[-1]
    if key not in KEYCODES:
        raise ValueError(f"不支持的按键: {key}（可用: {', '.join(KEYCODES)}）")
    required = 0
    forbidden = 0
    for m in parts[:-1]:
        if m not in MODIFIER_MASKS:
            raise ValueError(f"不支持的修饰键: {m}（可用: cmd/shift/ctrl/alt）")
        required |= MODIFIER_MASKS[m]
    for m in FORBIDDEN_MODS:
        if not (m in [p.strip().lower() for p in spec.split("+")[:-1]]):
            forbidden |= MODIFIER_MASKS[m]
    return KEYCODES[key], required, forbidden


def parse_cancel_key(spec):
    key = spec.strip().lower()
    if key not in KEYCODES:
        raise ValueError(f"不支持的取消键: {key}")
    return KEYCODES[key]


# ---------------- 主类 ----------------

class VoiceIME:
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

    # 双击判定窗口 / 最短有效录音（秒）
    CLICK_MS = 0.4
    MIN_HOLD_SEC = 0.4

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
        self.cancel_evt.clear()
        self.stop_evt.clear()
        self.free_mode = True
        self.free_exit_evt.clear()
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

    # ---------- 录音 ----------

    def _start_recording(self):
        gen = self._bump()
        self.cancel_evt.clear()
        self.stop_evt.clear()
        if not self._set_state("recording", gen):
            return
        path = self.tmp_dir / f"voice_ime_{int(time.time() * 1000)}.wav"
        t = threading.Thread(target=self._record_worker, args=(str(path), gen), daemon=True)
        t.start()

    def _record_to(self, path, stop_evt, use_silence=True):
        """录一段音频到 path；stop_evt 置位/静音(可选)/超时则停止。返回 (ok: bool|None, duration_sec)
        objc.autorelease_pool 确保 recorder 对象在函数返回时立即销毁、及时释放麦克风，
        避免上一次录音的输入设备被延迟释放导致下一次录音抓到静音。"""
        import objc
        from AVFoundation import AVAudioRecorder
        from Foundation import NSNumber, NSURL
        from CoreAudio import kAudioFormatLinearPCM
        settings = {
            "AVFormatIDKey": NSNumber(unsignedInt=kAudioFormatLinearPCM),
            "AVSampleRateKey": NSNumber(float=float(self.cfg["sample_rate"])),
            "AVNumberOfChannelsKey": NSNumber(int=1),
            "AVLinearPCMBitDepthKey": NSNumber(int=16),
            "AVLinearPCMIsBigEndianKey": NSNumber(bool=False),
            "AVLinearPCMIsFloatKey": NSNumber(bool=False),
        }
        with objc.autorelease_pool():
            res = AVAudioRecorder.alloc().initWithURL_settings_error_(
                NSURL.fileURLWithPath_(path), settings, None)
            rec = res[0] if isinstance(res, tuple) else res
            if rec is None or not rec.record():
                return (None, 0.0)
            rec.setMeteringEnabled_(True)
            start = time.time()
            silent_from = None
            while not stop_evt.is_set():
                time.sleep(0.25)
                if self.free_mode and self.free_exit_evt.is_set():
                    break  # 自由模式退出：立即结束本段
                now = time.time()
                if use_silence:
                    rec.updateMeters()
                    power = rec.averagePowerForChannel_(0)
                    if power is None:
                        continue
                    if power < float(self.cfg["silence_db"]):
                        if silent_from is None:
                            silent_from = now
                        elif now - silent_from >= float(self.cfg["silence_sec"]):
                            break
                    else:
                        silent_from = None
                if now - start >= float(self.cfg["max_duration"]):
                    break
            rec.stop()
            return (True, time.time() - start)

    def _record_worker(self, path, gen):
        play_sound("start")
        log("● 开始录音…（松开即发送，Esc 取消）")
        ok, dur = self._record_to(path, self.stop_evt, use_silence=False)
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
        if self._is_silent(path):
            self._skip_silent(path, gen)
            return
        play_sound("stop")
        self._process(path, gen)

    def _free_worker(self, gen):
        """自由说话模式：连续听写，停顿自动发送一段；连按两下退出"""
        play_sound("start")
        log("● 自由说话模式：直接说话，停顿自动发送；再连按两下退出")
        try:
            while not self.free_exit_evt.is_set():
                if not self._set_state("recording", gen):
                    return
                path = self.tmp_dir / f"voice_ime_free_{int(time.time() * 1000)}.wav"
                ok, dur = self._record_to(str(path), self.stop_evt)
                if ok is None:
                    play_sound("error")
                    log("✗ 无法录音（请检查 系统设置 → 隐私与安全性 → 麦克风）")
                    break
                if self.free_exit_evt.is_set():
                    self._cleanup(str(path))
                    break
                if dur < self.MIN_HOLD_SEC:
                    self._cleanup(str(path))
                    continue
                if self._is_silent(str(path)):
                    self._skip_silent(str(path))
                    continue
                play_sound("stop")
                self._process(str(path), gen)
        finally:
            self.free_mode = False
            self._set_state("idle", gen)
            play_sound("stop")
            log("自由说话模式已退出")

    # ---------- API 处理 ----------

    def _process(self, path, gen):
        if not self._set_state("processing", gen):
            return
        log("… 识别并优化中")
        try:
            text = self._api_audio(path, self.cfg["system_prompt"])
            if not text:
                raise RuntimeError("模型返回空文本")
            if self.cancel_evt.is_set() or self._get_state() != "processing":
                log("已取消本次输入")
                return
            if text in ("无语音", "无语音。", "[EMPTY]"):
                log("未检测到语音，已跳过")
                return
            log("✓ " + text)
            self.insert_text(text)
            play_sound("done")
        except Exception as e:
            play_sound("error")
            log(f"✗ 处理失败: {e}")
        finally:
            self._cleanup(path)
            self._set_state("idle", gen)

    def _api_audio(self, wav_path, instruction):
        b64 = base64.b64encode(Path(wav_path).read_bytes()).decode()
        model = self.cfg["model"]
        if model.startswith("qwen3-omni"):
            return self._responses_call("data:;base64," + b64, instruction)
        return self._chat_call("data:audio/wav;base64," + b64, instruction)

    def _http_json(self, url, payload, timeout):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.cfg["api_key"],
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _chat_call(self, audio_data_uri, instruction):
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        user_content = [
            {"type": "input_audio", "input_audio": {"data": audio_data_uri}},
            {"type": "text", "text": instruction},
        ]
        payload = {
            "model": self.cfg["model"],
            "messages": [
                {"role": "system", "content": self.cfg["system_prompt"]},
                {"role": "user", "content": user_content},
            ],
        }
        # 推理类模型用 max_completion_tokens；Gemini 等用 max_tokens
        if "gemini" in self.cfg["model"].lower():
            payload["max_tokens"] = int(self.cfg["max_tokens"])
        else:
            payload["max_completion_tokens"] = int(self.cfg["max_tokens"])
        last = None
        for attempt in range(3):
            try:
                data = self._http_json(url, payload, float(self.cfg["timeout"]))
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if content:
                    return content
                last = RuntimeError("模型返回空内容（上游抖动，已重试）")
            except Exception as e:
                last = e
            time.sleep(1.0)
        raise last

    def _responses_call(self, audio_data_uri, instruction):
        """qwen3-omni-*：/v1/responses 流式接口"""
        url = self.cfg["base_url"].rstrip("/") + "/responses"
        payload = {
            "model": self.cfg["model"],
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_data_uri, "format": "wav"}},
                    {"type": "text", "text": instruction},
                ],
            }],
            "stream": True,
            "stream_options": {"include_usage": True},
            "modalities": ["text"],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.cfg["api_key"],
                     "Content-Type": "application/json"},
        )
        last = None
        for attempt in range(3):
            text = []
            try:
                with urllib.request.urlopen(req, timeout=float(self.cfg["timeout"])) as r:
                    for line in r:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data: "):
                            try:
                                ev = json.loads(line[6:])
                            except Exception:
                                continue
                            if ev.get("type") == "response.output_text.delta":
                                text.append(ev.get("delta") or "")
                content = "".join(text).strip()
                if content:
                    return content
                last = RuntimeError("模型返回空内容（上游抖动，已重试）")
            except Exception as e:
                last = e
            time.sleep(1.0)
        raise last

    # ---------- 文本输入 ----------

    def insert_text(self, text):
        method = self.cfg.get("insert_method", "type")
        if method == "paste":
            self._insert_paste(text)
        else:
            self._insert_type(text)

    def _insert_type(self, text):
        from Quartz import (CGEventCreateKeyboardEvent, CGEventKeyboardSetUnicodeString,
                            CGEventPost, kCGHIDEventTap)
        for i in range(0, len(text), 50):
            chunk = text[i:i + 50]
            down = CGEventCreateKeyboardEvent(None, 0, True)
            CGEventKeyboardSetUnicodeString(down, len(chunk), chunk)
            CGEventPost(kCGHIDEventTap, down)
            up = CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(up, len(chunk), chunk)
            CGEventPost(kCGHIDEventTap, up)
            time.sleep(0.03)

    def _insert_paste(self, text):
        from AppKit import NSPasteboard, NSPasteboardTypeString
        from Quartz import (CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
                            kCGEventFlagMaskCommand, CGEventCreate, kCGEventSourceStateHIDSystemState)
        pb = NSPasteboard.generalPasteboard()
        saved = pb.pasteboardItems()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        for is_down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, 9, is_down)  # 9 = 'v'
            ev.setFlags_(kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, ev)
            time.sleep(0.02)
        time.sleep(0.1)
        try:
            if saved:
                pb.clearContents()
                pb.writeObjects_(saved)
        except Exception:
            pass

    # ---------- 事件监听 ----------

    def _tap_cb(self, proxy, etype, event, refcon):
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
            sys.exit(1)
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

    def _is_silent(self, path, peak_threshold=0.005):
        """本地检测 WAV 是否基本无语音（静音/纯环境音），避免空段发模型产生垃圾回复"""
        import array
        import wave
        try:
            with wave.open(path, "rb") as w:
                n = w.getnframes()
                if n < 200:
                    return True  # 不足 10ms，视为空
                frames = w.readframes(n)
            samples = array.array("h")
            samples.frombytes(frames)
            if not samples:
                return True
            # 大步采样找峰值（语音峰值通常 >0.05，环境噪声 <0.005）
            step = max(1, len(samples) // 20000)
            peak = 0
            for i in range(0, len(samples), step):
                a = abs(samples[i])
                if a > peak:
                    peak = a
                    if peak > 1000:
                        break
            return peak / 32768.0 < peak_threshold
        except Exception:
            return False  # 分析失败时放行，避免误删真实内容

    def _wav_peak(self, path):
        """读取 wav 峰值（0.0-1.0），失败返回 -1"""
        import array
        import wave
        try:
            with wave.open(path, "rb") as w:
                frames = w.readframes(w.getnframes())
            samples = array.array("h")
            samples.frombytes(frames)
            if not samples:
                return 0.0
            step = max(1, len(samples) // 20000)
            peak = max(abs(samples[i]) for i in range(0, len(samples), step))
            return round(peak / 32768.0, 4)
        except Exception:
            return -1.0

    def _skip_silent(self, path, gen=None):
        """静音段处理：保留/删除文件并记录。返回是否保留文件"""
        keep = bool(self.cfg.get("keep_skipped_audio"))
        if keep:
            log(f"未检测到语音，已跳过（peak={self._wav_peak(path)}，保留: {path}）")
        else:
            self._cleanup(path)
            log("未检测到语音，已跳过")
        if gen is not None:
            self._set_state("idle", gen)
        return keep

    def _cleanup(self, path):
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------- 自测 ----------------

def test_file(cfg, wav_path):
    ime = VoiceIME(cfg)
    log(f"发送 {wav_path} → {cfg['model']}")
    t0 = time.time()
    try:
        text = ime._api_audio(wav_path, cfg["system_prompt"])
        print(f"耗时 {time.time() - t0:.1f}s", flush=True)
        print("结果:", text, flush=True)
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)


def test_record(cfg):
    from AVFoundation import AVAudioRecorder
    from Foundation import NSNumber, NSURL
    from CoreAudio import kAudioFormatLinearPCM
    path = "/tmp/voice_ime_test.wav"
    if os.path.exists(path):
        os.remove(path)
    settings = {
        "AVFormatIDKey": NSNumber(unsignedInt=kAudioFormatLinearPCM),
        "AVSampleRateKey": NSNumber(float=float(cfg["sample_rate"])),
        "AVNumberOfChannelsKey": NSNumber(int=1),
        "AVLinearPCMBitDepthKey": NSNumber(int=16),
        "AVLinearPCMIsBigEndianKey": NSNumber(bool=False),
        "AVLinearPCMIsFloatKey": NSNumber(bool=False),
    }
    res = AVAudioRecorder.alloc().initWithURL_settings_error_(NSURL.fileURLWithPath_(path), settings, None)
    rec = res[0] if isinstance(res, tuple) else res
    if rec is None or not rec.record():
        print("录音失败（麦克风权限？）", file=sys.stderr)
        sys.exit(1)
    print("录音 3 秒…", flush=True)
    time.sleep(3)
    rec.stop()
    print("录音完成，发送到模型…", flush=True)
    test_file(cfg, path)


def pick_keycodes():
    """--keys：按下任意键打印其 keycode，用于配置 hotkey（含修饰键）"""
    from Quartz import (CGEventTapCreate, CGEventTapEnable, CGEventTapIsEnabled,
                        kCGHIDEventTap, kCGHeadInsertEventTap,
                        CGEventGetIntegerValueField, CGEventGetFlags,
                        kCGKeyboardEventKeycode,
                        CFMachPortCreateRunLoopSource, CFRunLoopAddSource,
                        CFRunLoopGetCurrent, kCFRunLoopDefaultMode, CFRunLoopRunInMode)
    mod_state = {}

    def cb(proxy, etype, event, refcon):
        if etype in (10, 11, 12):
            code = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            name = key_label(code)
            if etype == 10:
                kind = "按下"
            elif etype == 11:
                kind = "松开"
            else:  # flagsChanged：修饰键
                bit = MOD_FLAG_BITS.get(code)
                if bit is not None:
                    down = bool(CGEventGetFlags(event) & bit)
                else:
                    down = not mod_state.get(code, False)
                    mod_state[code] = down
                kind = "按下" if down else "松开"
            print(f"keycode: {code:<4} {name:<8} ({kind})", flush=True)
        return event

    tap = CGEventTapCreate(kCGHIDEventTap, kCGHeadInsertEventTap, 0,
                           (1 << 10) | (1 << 11) | (1 << 12), cb, None)
    if tap is None or not CGEventTapIsEnabled(tap):
        print("需要辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能", file=sys.stderr)
        sys.exit(1)
    CGEventTapEnable(tap, True)
    print("按键码拾取器：按下任意键显示 keycode（含 ⌘⇧⌥⌃ Fn 等修饰键），60 秒后自动退出（Ctrl+C 提前退出）", flush=True)
    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopDefaultMode)
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.3, False)
        except KeyboardInterrupt:
            break


def main():
    ap = argparse.ArgumentParser(description="macOS 语音输入法（热键触发，多模态模型转写+口语优化）")
    ap.add_argument("--test", action="store_true", help="录 3 秒并打印识别结果，不插入文本")
    ap.add_argument("--test-file", metavar="WAV", help="用已有 wav 文件测 API 链路")
    ap.add_argument("--keys", action="store_true", help="按键码拾取器：按下任意键打印 keycode")
    args = ap.parse_args()
    cfg = load_config()
    if args.test_file:
        test_file(cfg, args.test_file)
    elif args.test:
        test_record(cfg)
    elif args.keys:
        pick_keycodes()
    else:
        VoiceIME(cfg).run()


if __name__ == "__main__":
    main()
