"""日志与系统音效。纯逻辑（afplay 子进程）。"""
import subprocess
import time

SOUNDS = {"start": "Glass", "stop": "Tink", "done": "Ping", "error": "Basso"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def play_sound(name):
    try:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{SOUNDS[name]}.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
