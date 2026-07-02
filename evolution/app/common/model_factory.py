"""Agent 的 LLM 模型工厂（D7：复用 judge 配置）。

evolution 端引入 langchain ChatOpenAI，指向现有 judge 配置
（judge_model / judge_api_key / judge_base_url），供 create_deep_agent 使用。
evolve / eval_agent 两个 Agent 都用本工厂构建模型，复用同一套
deepseek/openai 兼容端点 + API key，不新增配置。
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.core.settings import settings


def build_agent_model(*, temperature: float = 0.2) -> BaseChatModel:
    """构建 Agent 用的 ChatModel（复用 judge 配置，D7）。

    Args:
        temperature: 温度（Agent 决策需要一定探索性，默认 0.2）

    Returns:
        BaseChatModel 实例（给 create_deep_agent）

    Raises:
        RuntimeError: judge 配置缺失
    """
    if not (settings.judge_model and settings.judge_api_key):
        raise RuntimeError(
            "Agent 模型未配置：需设置 JUDGE_MODEL / JUDGE_API_KEY"
            "（复用 judge 配置，D7）"
        )

    base_url = (
        settings.judge_base_url.rstrip("/")
        if settings.judge_base_url
        else "https://api.openai.com/v1"
    )
    # model 可能是 "openai:gpt-4o-mini" 或 "gpt-4o-mini"，去掉 provider 前缀
    model = settings.judge_model.split(":", 1)[-1]

    return ChatOpenAI(
        model=model,
        api_key=settings.judge_api_key,
        base_url=base_url,
        temperature=temperature,
    )


__all__ = ["build_agent_model"]
