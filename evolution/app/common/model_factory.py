"""Agent 的 LLM 模型工厂（D7：复用 judge 配置）。

evolution 端引入 langchain ChatOpenAI，供 create_deep_agent 使用。
evolve / eval_agent 两个 Agent 都用本工厂构建模型，复用同一套
deepseek/openai 兼容端点 + API key。

桌面化改造（2026-07-07）：配置不再从 settings.judge_* 读，改从 llm_config 表读
（桌面端填 → HTTP → evolution 加密存）。judge 与 agent 共用同一套配置（合一）。
"""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.core import db

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


def build_agent_model(*, temperature: float = 0.2, scope: str = "evolution") -> BaseChatModel:
    """构建 Agent 用的 ChatModel（从 llm_config 表读配置）。

    Args:
        temperature: 温度（Agent 决策需要一定探索性，默认 0.2）
        scope: 模型配置作用域。FR-007 判评分离：
            - "evolution"（默认）：evolve agent 用
            - "eval"：eval_agent 用，与 evolution 异家族根治 PLS（EVD-004）
            - "executor"：写作 agent 用（本工厂不直接用，保留）
            eval scope 未配置时降级用 evolution scope 并警告（EDGE-005，不阻塞）。

    Returns:
        BaseChatModel 实例（给 create_deep_agent）

    Raises:
        RuntimeError: LLM 未配置（llm_config 表无 key 且无降级路径）
    """
    import logging
    logger = logging.getLogger("evolution.common.model_factory")

    config = db.LlmConfigsRepository.get_active(scope)
    effective_scope = scope
    if config is None:
        if scope == "eval":
            # EDGE-005：eval scope 未配置 → 降级用 evolution scope（不阻塞，警告 PLS 风险）
            logger.warning(
                "eval scope 未配置，降级用 evolution scope 模型（PLS 风险：评估与 writer/evolve "
                "可能同家族，缺失性缺陷会被同源偏好静默放过）。"
                "请在桌面端为 eval scope 配置异家族模型以根治 Preference Leakage。"
            )
            config = db.LlmConfigsRepository.get_active("evolution")
            effective_scope = "evolution"
        if config is None:
            raise RuntimeError(
                f"Agent 模型未配置：scope={scope}（及降级 evolution）在 llm_config 表均无激活配置。"
                "请在桌面端「进化端模型」页填写大模型 API（base_url / api_key / model）"
            )
    api_key, base_url_raw, model_raw = config

    # 启动期同家族警告（EDGE-005 第二档：配了但同家族）。不阻塞，只标 PLS 风险。
    if effective_scope == "eval":
        _warn_eval_same_family(model_raw)

    # 关闭 deepagents 默认注入的 general-purpose 子代理（详见模块顶部说明）。
    _ensure_gp_disabled()

    base_url = base_url_raw.rstrip("/")
    # model 可能是 "openai:gpt-4o-mini" 或 "gpt-4o-mini"，去掉 provider 前缀
    model = model_raw.split(":", 1)[-1]

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        # DeepSeek-chat 的单次输出硬上限为 8192 token，无法靠调大突破。
        # 这里显式声明，既自文档化，也防止切到默认上限更小的兼容端点时静默截断。
        # 注意：execute 子代理写大体积源码时仍可能撞此上限——治本之策是其在
        # prompt 里的「单次单文件」铁律（串行写入，避免单次响应塞多个文件）。
        max_tokens=8192,
        # 防止单次模型调用无限期挂起/无限重试。
        #
        # 300s 超时：进化 Agent 要产出大体积源码（write_middleware/write_tool/
        # write_subagent 参数就是整个文件）+ design_doc + change_log，单次响应
        # 经常贴着 max_tokens=8192 跑，DeepSeek 兼容端点实测 30~50s 是常态，
        # 叠加偶发抖动常超 60s。原 60s 仅对评估 Agent（小体积 JSON findings）够用，
        # 对进化 Agent 稳定撞线报 APITimeoutError。300s 给足余量，对评估 Agent
        # 无副作用（它实际几秒返回，timeout 只是上限）。max_retries=1 保留——
        # 真断网/服务端 5xx 时仍能及时放弃，不会无限挂起。
        request_timeout=300,
        max_retries=1,
    )


def _model_family(model_raw: str) -> str:
    """从模型名粗判家族（同家族=同底座，判评分离要异家族）。

    判评分离（DEC-007）要求 eval 与 writer/evolve 异家族。model 名的前缀通常标明
    底座（deepseek/glm/qwen/gpt/claude 等）。这里提取前缀作家族签名。
    本判定是粗粒度的——精确的同家族检测需对照 writer/evolve 的实际激活模型。
    """
    name = (model_raw or "").lower().split(":", 1)[-1]
    for fam in ("deepseek", "glm", "qwen", "gpt", "claude", "llama", "mistral", "gemini"):
        if fam in name:
            return fam
    return name.split("-")[0] if name else "unknown"


def _warn_eval_same_family(eval_model_raw: str) -> None:
    """eval 与 writer/evolve 同家族时警告（EDGE-005，不阻塞）。

    判评分离只切 eval↔evolution 边；evolution scope 是 evolve agent 用的，
    writer 用 executor scope（默认是 evolution 副本）。三者任一同家族都有 PLS 风险。
    """
    import logging
    logger = logging.getLogger("evolution.common.model_factory")
    eval_fam = _model_family(eval_model_raw)
    for other_scope in ("evolution", "executor"):
        other = db.LlmConfigsRepository.get_active(other_scope)
        if other is None:
            continue
        other_fam = _model_family(other[2])
        if other_fam == eval_fam:
            logger.warning(
                "PLS 风险：eval 模型（%s，家族=%s）与 %s scope 模型（%s，家族=%s）同家族。"
                "Preference Leakage 会让评估对同源输出放水，缺失性缺陷可能被静默放过。"
                "建议 eval scope 配置异家族模型。",
                eval_model_raw, eval_fam, other_scope, other[2], other_fam,
            )


__all__ = ["build_agent_model"]
