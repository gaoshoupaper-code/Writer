"""LLM 调用：调 OpenAI 兼容的 chat completions API（deepseek / openai 等）。

故意不依赖 langchain/openai-sdk，用 httpx 直调兼容协议，保持 monitoring 独立轻量。
model 配置见 settings：judge_model / judge_api_key / judge_base_url。
"""

from __future__ import annotations

import httpx

from app.settings import settings


def judge_enabled() -> bool:
    """LLM-judge 是否可用（需配置 model + api_key）。"""
    return bool(settings.judge_model and settings.judge_api_key)


def chat(messages: list[dict[str, str]], *, temperature: float = 0.0, timeout: float = 60.0) -> str:
    """调一次 chat completion，返回 assistant 文本。

    messages: OpenAI 格式 [{"role":"system","content":...}, {"role":"user","content":...}]
    兼容 deepseek / openai / 任何 OpenAI 兼容端点。
    """
    if not judge_enabled():
        raise RuntimeError("LLM-judge 未配置（JUDGE_MODEL/JUDGE_API_KEY 为空）")

    base_url = settings.judge_base_url.rstrip("/") if settings.judge_base_url else "https://api.openai.com/v1"
    url = f"{base_url}/chat/completions"
    # model 可能是 "openai:gpt-4o-mini" 或 "gpt-4o-mini"，去掉 provider 前缀
    model = settings.judge_model.split(":", 1)[-1]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    headers = {
        "Authorization": f"Bearer {settings.judge_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    # OpenAI 兼容格式：choices[0].message.content
    return data["choices"][0]["message"]["content"]
