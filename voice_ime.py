#!/usr/bin/env python3
"""SSR 语音输入法入口（薄壳）。逻辑见 ssr/ 包，配置见 config.json。

用法:
  .venv/bin/python voice_ime.py                 # 常驻运行，监听全局热键
  .venv/bin/python voice_ime.py --test          # 录 3 秒 → API → 打印结果（不插入）
  .venv/bin/python voice_ime.py --test-file x.wav  # 用已有音频文件测 API 链路
  .venv/bin/python voice_ime.py --keys          # 按键码拾取器（配置 hotkey 用）
"""
import argparse

from ssr.config import load_config
from ssr.engine import VoiceIME
from ssr.tools import pick_keycodes, test_file, test_record


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
