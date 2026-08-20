"""录音适配层。ADAPTER（AVAudioRecorder / CoreAudio）。

objc.autorelease_pool 确保 recorder 对象在函数返回时立即销毁、及时释放麦克风，
避免上一次录音的输入设备被延迟释放导致下一次录音抓到静音。
"""
import time


def create_recorder(path, sample_rate):
    """创建并开始录音的 AVAudioRecorder；失败返回 None"""
    import objc
    from AVFoundation import AVAudioRecorder
    from Foundation import NSNumber, NSURL
    from CoreAudio import kAudioFormatLinearPCM

    settings = {
        "AVFormatIDKey": NSNumber(unsignedInt=kAudioFormatLinearPCM),
        "AVSampleRateKey": NSNumber(float=float(sample_rate)),
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
            return None
        rec.setMeteringEnabled_(True)
        return rec


def record_to(path, stop_evt, sample_rate, use_silence=True,
              silence_db=-45.0, silence_sec=1.5, max_duration=60.0):
    """录一段音频到 path；stop_evt 置位/静音(可选)/超时则停止。
    返回 (ok: bool|None, duration_sec)"""
    rec = create_recorder(path, sample_rate)
    if rec is None:
        return (None, 0.0)
    start = time.time()
    silent_from = None
    try:
        while not stop_evt.is_set():
            time.sleep(0.25)
            now = time.time()
            if use_silence:
                rec.updateMeters()
                power = rec.averagePowerForChannel_(0)
                if power is None:
                    continue
                if power < float(silence_db):
                    if silent_from is None:
                        silent_from = now
                    elif now - silent_from >= float(silence_sec):
                        break
                else:
                    silent_from = None
            if now - start >= float(max_duration):
                break
    finally:
        rec.stop()
    return (True, time.time() - start)


def calibrate_ambient(rec):
    """0.5s 环境标定：取 metering 的 75% 分位作为底噪（dB）。
    用噪声上沿而非均值/低分位，避免瞬时数字静音（-120）拉低阈值导致分段失效"""
    powers = []
    t0 = time.time()
    while time.time() - t0 < 0.5:
        rec.updateMeters()
        p = rec.averagePowerForChannel_(0)
        if p is not None:
            powers.append(p)
        time.sleep(0.05)
    if not powers:
        return -50.0
    powers.sort()
    idx = min(len(powers) - 1, max(0, int(len(powers) * 0.75)))
    return max(min(powers[idx], -30.0), -80.0)
