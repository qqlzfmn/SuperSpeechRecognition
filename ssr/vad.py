"""WAV 分析：帧级 VAD、峰值、切片。纯 stdlib（wave/array/struct），无框架依赖。"""
import array
import struct
import time
import wave

from .sounds import log


def wav_voiced_sec(path, threshold):
    """返回 (voiced_sec, total_sec)。按 25ms 帧计算 RMS，RMS > threshold 记为有声帧。
    threshold 为帧 RMS 幅值（0-1）。失败返回 (0.0, 0.0)"""
    try:
        with wave.open(path, "rb") as w:
            sr = w.getframerate() or 16000
            n = w.getnframes()
            if n < int(sr * 0.05):
                return (0.0, 0.0)  # <50ms
            frames = w.readframes(n)
        samples = array.array("h")
        samples.frombytes(frames)
        if not samples:
            return (0.0, 0.0)
        frame_len = max(1, int(sr * 0.025))
        voiced = 0
        total = 0
        i = 0
        L = len(samples)
        while i + frame_len // 2 <= L:
            seg = samples[i:i + frame_len]
            s = 0.0
            cnt = 0
            for v in seg[::4]:  # 抽稀 4 倍估算 RMS，省算力
                s += v * v
                cnt += 1
            rms = (s / cnt) ** 0.5 if cnt else 0.0
            if rms / 32768.0 >= threshold:
                voiced += 1
            total += 1
            i += frame_len
        return (voiced * 0.025, total * 0.025)
    except Exception:
        return (0.0, 0.0)


def is_silent(path, threshold, keep_ms):
    """帧级 VAD：有声时长 < keep_ms（毫秒）视为无语音。"""
    voiced, _ = wav_voiced_sec(path, threshold)
    return voiced < float(keep_ms) / 1000.0


def wav_peak(path):
    """读取 wav 峰值（0.0-1.0），失败返回 -1"""
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


def slice_wav(src, t0, t1, dst, sample_rate):
    """从正在写入的会话 wav 切出 [t0, t1] 秒的 PCM 并写成独立 wav。成功返回 True"""
    sr = int(sample_rate)
    try:
        with open(src, "rb") as f:
            hdr = f.read(12)
            if hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
                return False
            data_off = None
            while True:  # 扫描 chunk 定位 data 区
                cid = f.read(4)
                if len(cid) < 4:
                    break
                csize = struct.unpack("<I", f.read(4))[0]
                if cid == b"data":
                    data_off = f.tell()
                    break
                f.seek(csize + (csize & 1), 1)
            if data_off is None:
                return False
            n0 = int(t0 * sr) * 2
            n1 = int(t1 * sr) * 2
            if n1 <= n0:
                return False
            want = n1 - n0
            data = b""
            for _ in range(6):  # 写入有缓冲，重试补读
                f.seek(data_off + n0)
                data = f.read(want)
                if len(data) >= want:
                    break
                time.sleep(0.2)
            if len(data) < want:
                log("分段读取不完整，已丢弃")
                return False
        with open(dst, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", 36 + len(data)))
            f.write(b"WAVEfmt ")
            f.write(struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16))
            f.write(b"data")
            f.write(struct.pack("<I", len(data)))
            f.write(data)
        return True
    except Exception as e:
        log(f"分段失败: {e}")
        return False
