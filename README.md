# SSR · SuperSpeechRecognition

macOS 语音输入法：**全局热键触发录音 → 多模态大模型转写 + 口语优化 → 自动输入到当前应用**。无 GUI，纯命令行常驻。

> SSR = Super Speech Recognition（项目简称）

## 特性

- **按住说话**：按住触发键录音，松开自动转写并输入到光标所在位置
- **自由说话模式**：双击触发键开启，连续收音、停顿自动分段发送，再双击退出——整个会话只获取一次麦克风，段间零丢话、无提示音刷屏
- **口语自动优化**：多模态模型直接理解音频，去除语气词/口癖、修正口误、补全标点
- **本地语音活性检测（VAD）**：帧级 RMS 判定 + 环境自适应标定，静音段不进 API（省调用、防垃圾输出）
- **静音段/无语音保护**：本地能量检测 + 模型"无语音"哨兵双保险
- 快捷键与模型全部可配置（`config.json`）

## 工作原理

```
全局热键(CGEventTap) → 录音(AVAudioRecorder) → 本地 VAD → 多模态模型音频理解 → 文本插入(CGEvent/NSPasteboard)
```

自由说话模式：单 recorder 连续录音，VAD 检测静音后按时间戳切片发送，段切片入 FIFO 队列并行转写、保序插入。

## 安装

```bash
git clone https://github.com/qqlzfmn/SuperSpeechRecognition.git
cd SuperSpeechRecognition
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.json.example config.json   # 填入你的 API Key
```

**权限**（系统设置 → 隐私与安全性，授予你的终端 App）：
- **麦克风**：录音必需
- **辅助功能**：全局热键监听与模拟输入必需

## 使用

```bash
.venv/bin/python voice_ime.py                  # 常驻运行，Ctrl+C 退出
.venv/bin/python voice_ime.py --test           # 录 3 秒 → 识别 → 打印（不插入）
.venv/bin/python voice_ime.py --test-file x.wav  # 用已有音频测 API 链路
.venv/bin/python voice_ime.py --keys           # 按键码拾取器（配置热键用）
```

| 操作 | 行为 |
|---|---|
| **按住触发键** | 开始录音，松开自动转写输入（默认 右⌘，键码 54） |
| **连按两下** | 开关自由说话模式（停顿 1.5s 自动分段发送） |
| **Esc** | 取消当前录音 / 退出自由模式 / 丢弃待插入结果 |

## 配置（config.json）

| 字段 | 默认 | 说明 |
|---|---|---|
| `model` | `qwen3-omni-flash-all` | 多模态模型：`mimo-v2.5` / `gemini-2.5-flash` 亦可（音频经 `data:audio/wav;base64,` 前缀输入） |
| `hotkey` | `54` | 触发键 keycode（数字或 `right_cmd` 等名称） |
| `trigger` | `hold` | `hold`=按住说话+双击自由模式；`toggle`=按一下开始/再按停止 |
| `silence_sec` | `1.5` | 自由模式分段静音阈值（秒） |
| `vad_threshold` | `0.004` | 帧 RMS 语音阈值（0-1 满幅），安静语音(~0.01 峰值)仍可检出 |
| `vad_keep_ms` | `250` | 保留录音所需最少有声时长 |
| `insert_method` | `type` | `type`=模拟键盘输入；`paste`=剪贴板粘贴 |
| `log_keys` | `false` | 打印每次按键事件（排错） |
| `keep_skipped_audio` | `false` | 保留被判静音的录音并记录峰值（诊断） |

## 架构与迁移

实现 = **纯逻辑层**（Python stdlib，无框架依赖）+ **薄适配层**（PyObjC 直绑 Apple 框架，方法标注 `# ADAPTER`）。

| 适配层 | Swift 原生对应 |
|---|---|
| `AVAudioRecorder` + metering | `AVAudioEngine` tap |
| `Quartz CGEventTap`（flagsChanged） | `CGEventTap` |
| `CGEvent` / `NSPasteboard` | 同名框架 |

逻辑层（VAD、分段、队列、状态机）零第三方依赖，可直接翻译。规划中的原生 macOS App 由此迁移。

## 注意

- `config.json` 含 API Key，已 gitignore；公开仓库只含占位模板 `config.json.example`
- 模拟输入会把文本打进当前焦点应用——焦点在终端时文本会成为命令，请先点进目标输入框
- API 通道：[dmxapi.cn](https://www.dmxapi.cn)（OpenAI 兼容）；`mimo-v2.5` 音频输入需 `data:audio/wav;base64,` 前缀格式
