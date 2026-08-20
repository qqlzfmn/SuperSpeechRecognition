"""热键映射与解析。纯逻辑，无框架依赖。"""
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
