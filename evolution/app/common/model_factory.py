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

# 禁用 deepagents 自动注入的 general-purpose 子代理。
#
# 原因：create_deep_agent 即使 subagents=None，也会自动注入一个默认的
# general-purpose 子代理并暴露 task 工具（deepagents/graph.py 的 GP 注入逻辑）。
# 这让 plan/execute 等叶子子代理能再嵌套委托——LLM 一旦用它去探索环境，
# 嵌套子代理静默卡死、永远没有 tool_end（trace 20260705-1507 卡在 #195 的根因）。
#
# 做法：注册 HarnessProfile 关掉 general-purpose。这对所有 Agent 都正确——
#   - plan/execute/eval：本就不该有 task，去掉后无法再嵌套委托；
#   - driver：靠 subagents=[plan, execute] 走 SubAgentMiddleware 注入 task，
#     不依赖 general-purpose，去掉无副作用（trace 验证 driver 只委托 plan/execute）。
#
# register_harness_profile 是 additive 且幂等的，进程内首次构建模型时注册一次即可。
_GP_DISABLED_REGISTERED = False


def _ensure_gp_disabled() -> None:
    """注册 HarnessProfile 禁用 general-purpose 子代理（幂等，仅注册一次）。"""
    global _GP_DISABLED_REGISTERED
    if _GP_DISABLED_REGISTERED:
        return
    from deepagents import (
        GeneralPurposeSubagentProfile,
        HarnessProfileConfig,
    )
    from deepagents.profiles import register_harness_profile

    register_harness_profile(
        "openai",  # langchain ChatOpenAI 的 ls provider
        HarnessProfileConfig(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    _GP_DISABLED_REGISTERED = True


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

    # 关闭 deepagents 默认注入的 general-purpose 子代理（详见模块顶部说明）。
    _ensure_gp_disabled()

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
        # DeepSeek-chat 的单次输出硬上限为 8192 token，无法靠调大突破。
        # 这里显式声明，既自文档化，也防止切到默认上限更小的兼容端点时静默截断。
        # 注意：execute 子代理写大体积源码时仍可能撞此上限——治本之策是其在
        # prompt 里的「单次单文件」铁律（串行写入，避免单次响应塞多个文件）。
        max_tokens=8192,
        # 防止单次模型调用无限期挂起/无限重试。
        # 单请求 60s 超时（含 deepseek 等兼容端点的偶发抖动）；503 等最多重试 1 次。
        request_timeout=60,
        max_retries=1,
    )


__all__ = ["build_agent_model"]
