"""文本输入：模拟键盘 / 剪贴板粘贴。ADAPTER（Quartz CGEvent / AppKit NSPasteboard）。"""
import time


class TextInserter:
    def __init__(self, method="type"):
        self.method = method  # type | paste

    def insert(self, text):
        if self.method == "paste":
            self._insert_paste(text)
        else:
            self._insert_type(text)

    # ---------- ADAPTER ----------

    def _insert_type(self, text):  # ADAPTER（Quartz CGEvent 模拟键盘）
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

    def _insert_paste(self, text):  # ADAPTER（AppKit NSPasteboard + Quartz）
        from AppKit import NSPasteboard, NSPasteboardTypeString
        from Quartz import (CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
                            kCGEventFlagMaskCommand)
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
