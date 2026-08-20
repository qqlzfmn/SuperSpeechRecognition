"""配置加载：默认值 + config.json 覆盖。纯逻辑，无框架依赖。"""
import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

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
    "vad_threshold": 0.004,   # 帧 RMS 语音阈值（0-1 满幅，约 -48dBFS；安静语音峰值~0.01 仍可检出）
    "vad_keep_ms": 250,       # 保留录音所需的最少有声时长（毫秒）
    "insert_method": "type",  # type=模拟键盘输入 | paste=剪贴板粘贴
    "max_tokens": 1000,
    "timeout": 240,
    "log_keys": False,            # true = 在日志里打印每次按键事件（排错用）
    "keep_skipped_audio": False,  # true = 保留被判为静音/无语音的录音文件并记录峰值（诊断用）
}


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
