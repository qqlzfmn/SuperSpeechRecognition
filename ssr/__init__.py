"""SSR — SuperSpeechRecognition，macOS 语音输入法核心包。

架构：纯逻辑层（stdlib，无框架依赖）+ 薄适配层（PyObjC 直绑 Apple 框架，标记 ADAPTER）。
迁移 Swift 时适配层 1:1 映射同名框架 API，逻辑层机械翻译。
"""
from .config import DEFAULTS, load_config
from .engine import VoiceIME

__all__ = ["VoiceIME", "load_config", "DEFAULTS"]
__version__ = "0.2.0"
