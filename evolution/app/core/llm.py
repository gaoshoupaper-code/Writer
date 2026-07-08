"""LLM 调用：调 OpenAI 兼容的 chat completions API（deepseek / openai 等）。

故意不依赖 langchain/openai-sdk，用 httpx 直调兼容协议，保持 evolution 独立轻量。

桌面化改造（2026-07-07）：配置不再从 settings.judge_* 读，改从 llm_config 表读
（桌面端填 → HTTP → evolution 加密存）。见 app/core/db.py 的 LlmConfigRepository。
"""

from __future__ import annotations

import httpx

from app.core import db


def judge_enabled() -> bool:
    """LLM-judge 是否可用（llm_config 表已配置 api_key + base_url + model）。"""
    return db.LlmConfigRepository.get_active() is not None


def _get_config() -> tuple[str, str, str]:
    """读取当前 LLM 配置（api_key, base_url, model）。未配置抛 RuntimeError。"""
    config = db.LlmConfigRepository.get_active()
    if config is None:
        raise RuntimeError(
            "LLM 未配置。请在桌面端「配置」页填写大模型 API（base_url / api_key / model）。"
        )
    return config


def chat(messages: list[dict[str, str]], *, temperature: float = 0.0, timeout: float = 60.0) -> str:
    """调一次 chat completion，返回 assistant 文本。

    messages: OpenAI 格式 [{"role":"system","content":...}, {"role":"user","content":...}]
    兼容 deepseek / openai / 任何 OpenAI 兼容端点。
    """
    api_key, base_url_raw, model_raw = _get_config()
    base_url = base_url_raw.rstrip("/")
    url = f"{base_url}/chat/completions"
    # model 可能是 "openai:gpt-4o-mini" 或 "gpt-4o-mini"，去掉 provider 前缀
    model = model_raw.split(":", 1)[-1]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    # OpenAI 兼容格式：choices[0].message.content
    return data["choices"][0]["message"]["content"]
