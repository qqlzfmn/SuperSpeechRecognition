"""多模态模型音频理解客户端（OpenAI 兼容）。纯逻辑，urllib，无框架依赖。"""
import base64
import json
import time
import urllib.request
from pathlib import Path

# 模型对"无语音"音频的哨兵回复，视为空结果
SENTINELS = ("无语音", "无语音。", "[EMPTY]")


class ApiClient:
    def __init__(self, base_url, api_key, model, system_prompt,
                 max_tokens=1000, timeout=240):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)

    # ---------- 对外 ----------

    def transcribe(self, wav_path, instruction=None):
        """音频文件 → 润色后文本"""
        b64 = base64.b64encode(Path(wav_path).read_bytes()).decode()
        instruction = instruction or self.system_prompt
        if self.model.startswith("qwen3-omni"):
            return self._responses_call("data:;base64," + b64, instruction)
        return self._chat_call("data:audio/wav;base64," + b64, instruction)

    def is_empty_reply(self, text):
        """模型哨兵回复（音频中无语音）"""
        return text in SENTINELS

    # ---------- 内部 ----------

    def _http_json(self, url, payload, timeout):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def _chat_call(self, audio_data_uri, instruction):
        url = self.base_url + "/chat/completions"
        user_content = [
            {"type": "input_audio", "input_audio": {"data": audio_data_uri}},
            {"type": "text", "text": instruction},
        ]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        # 推理类模型用 max_completion_tokens；Gemini 等用 max_tokens
        if "gemini" in self.model.lower():
            payload["max_tokens"] = self.max_tokens
        else:
            payload["max_completion_tokens"] = self.max_tokens
        last = None
        for attempt in range(3):
            try:
                data = self._http_json(url, payload, self.timeout)
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if content:
                    return content
                last = RuntimeError("模型返回空内容（上游抖动，已重试）")
            except Exception as e:
                last = e
            time.sleep(1.0)
        raise last

    def _responses_call(self, audio_data_uri, instruction):
        """qwen3-omni-*：/v1/responses 流式接口"""
        url = self.base_url + "/responses"
        payload = {
            "model": self.model,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": audio_data_uri, "format": "wav"}},
                    {"type": "text", "text": instruction},
                ],
            }],
            "stream": True,
            "stream_options": {"include_usage": True},
            "modalities": ["text"],
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        last = None
        for attempt in range(3):
            text = []
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    for line in r:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data: "):
                            try:
                                ev = json.loads(line[6:])
                            except Exception:
                                continue
                            if ev.get("type") == "response.output_text.delta":
                                text.append(ev.get("delta") or "")
                content = "".join(text).strip()
                if content:
                    return content
                last = RuntimeError("模型返回空内容（上游抖动，已重试）")
            except Exception as e:
                last = e
            time.sleep(1.0)
        raise last
