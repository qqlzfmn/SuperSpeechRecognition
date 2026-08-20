"""自测与工具：--test / --test-file / --keys。"""
import sys
import time

from . import recorder as rec_mod
from .api import ApiClient
from .hotkey import MOD_FLAG_BITS, key_label
from .sounds import log


def test_file(cfg, wav_path):
    api = ApiClient(cfg["base_url"], cfg["api_key"], cfg["model"],
                    cfg["system_prompt"], cfg["max_tokens"], cfg["timeout"])
    log(f"发送 {wav_path} → {cfg['model']}")
    t0 = time.time()
    try:
        text = api.transcribe(wav_path, cfg["system_prompt"])
        print(f"耗时 {time.time() - t0:.1f}s", flush=True)
        print("结果:", text, flush=True)
    except Exception as e:
        print(f"失败: {e}", file=sys.stderr)


def test_record(cfg):
    import os
    path = "/tmp/voice_ime_test.wav"
    if os.path.exists(path):
        os.remove(path)
    rec = rec_mod.create_recorder(path, float(cfg["sample_rate"]))
    if rec is None:
        print("录音失败（麦克风权限？）", file=sys.stderr)
        sys.exit(1)
    print("录音 3 秒…", flush=True)
    time.sleep(3)
    rec.stop()
    print("录音完成，发送到模型…", flush=True)
    test_file(cfg, path)


def pick_keycodes():  # ADAPTER（Quartz CGEventTap 按键码拾取器）
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
