"""CreditsMiddleware — 积分计费中间件（AD2/AD6）。

与 TraceMiddleware 并列挂在 agent 装配链上，职责分离：
- TraceMiddleware 管"记录"（trace 事件）
- CreditsMiddleware 管"计费"（预扣/累加/强停）

两个拦截点（AD6）：
1. awrap_tool_call：检测 task(storybuilding) 委托 → 触发预扣（D13）
2. awrap_model_call：提取 usage → 折算积分 → 累加消耗 → 检查强停（D3/D27）

不挂载场景：interview 子代理（访谈免费）、A/B 测试/管理员路径（credits_service=None）。

异常处理：
- CreditExhaustedError：余额触及 max_debt，中断 agent 执行（D27 强停）。
- 预扣失败（余额不足）：在 tool_call 阶段抛出，阻止 storybuilding 启动。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ModelResponse

from .exceptions import CreditExhaustedError, InsufficientCreditsError
from .service import CreditsService
from .tier_parser import parse_demand_file

logger = logging.getLogger("writer.credits.middleware")

# 触发预扣的子代理类型：首次委托这些子代理 = 访谈结束、正式创作开始
_CREATION_SUBAGENTS = {"storybuilding", "detail_outline", "writing"}


class CreditsMiddleware(AgentMiddleware):
    """积分计费中间件。

    每次创作会话（thread）只有一个 CreditsMiddleware 实例挂在 meta agent 上，
    负责整个创作生命周期的计费。子代理的中间件实例共享同一份 hold 状态
    （通过 thread_id 关联）。

    Args:
        credits_service: CreditsService 实例
        trace_id: 当前 trace 标识（记录到 hold）
        owner_id: 用户 ID
        workspace_path: workspace 路径（读 demand.md 用）
        agent_name: 当前 agent 名称
    """

    def __init__(
        self,
        credits_service: CreditsService,
        trace_id: str | None,
        owner_id: str,
        workspace_path: Path,
        agent_name: str,
    ) -> None:
        self._service = credits_service
        self._trace_id = trace_id
        self._owner_id = owner_id
        self._workspace_path = workspace_path
        self._agent_name = agent_name

    # ------------------------------------------------------------------
    # 模型调用拦截：提取 usage → 折算 → 累加 → 强停检查
    # ------------------------------------------------------------------

    def wrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        response = handler(request)
        self._process_model_response(response)
        return response

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        response = await handler(request)
        self._process_model_response(response)
        return response

    def _process_model_response(self, response: Any) -> None:
        """从模型响应提取 usage，折算积分，累加到 hold，检查强停。"""
        hold = self._get_or_load_active_hold()
        if hold is None:
            return  # 无活跃预扣（interview 阶段 / 非创作路径），跳过

        usage = _extract_usage(response)
        if usage is None:
            return  # 拿不到 usage（如 deepseek 流式未返回），跳过本次

        input_tokens = usage.get("input_tokens") or 0
        output_tokens = usage.get("output_tokens") or 0
        cached_tokens = _extract_cached_tokens(response)

        credits = self._service._config.calculate_credits(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
        if credits > 0:
            self._service.add_consumption(hold["hold_id"], credits)

        # D27 强停检查
        updated_hold = self._service.get_active_hold(hold["thread_id"]) or hold
        if self._service.check_credit_limit(self._owner_id, updated_hold):
            logger.warning(
                "强停触发：user=%s hold=%s consumed=%d 触及负债上限",
                self._owner_id, hold["hold_id"], updated_hold.get("consumed", 0),
            )
            # 结算并强停
            self._service.settle_hold(hold["hold_id"], force_stopped=True)
            raise CreditExhaustedError(
                f"积分耗尽，创作已强制停止。本次消耗 {updated_hold.get('consumed', 0)} 积分。"
            )

    # ------------------------------------------------------------------
    # 工具调用拦截：检测 storybuilding 委托 → 触发预扣
    # ------------------------------------------------------------------

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check_and_create_hold_for_tool(request)
        return handler(request)

    async def awrap_tool_call(
        self, request: Any, handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        self._check_and_create_hold_for_tool(request)
        return await handler(request)

    def _check_and_create_hold_for_tool(self, request: Any) -> None:
        """检测 task(storybuilding) 委托，首次出现时触发预扣（D13）。"""
        tool_call = getattr(request, "tool_call", None)
        if tool_call is None:
            return

        tool_name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
        if tool_name != "task":
            return

        # 提取 subagent_type
        args = tool_call.get("args") if isinstance(tool_call, dict) else getattr(tool_call, "args", None)
        subagent_type = _extract_subagent_type(args)
        if subagent_type not in _CREATION_SUBAGENTS:
            return

        # 已有活跃预扣则跳过（续创/多次委托）
        thread_id = _extract_thread_id_from_config()
        if thread_id and self._service.get_active_hold(thread_id):
            return

        # 解析 demand.md：status 必须是 confirmed，且能解析出篇幅档位
        demand_path = self._workspace_path / "demand.md"
        status, tier = parse_demand_file(demand_path)
        if status != "confirmed":
            logger.warning("预扣跳过：demand.md status=%s（非 confirmed）", status)
            return
        if tier is None:
            logger.warning("预扣跳过：无法解析篇幅档位，降级为档位1")
            tier = 1

        if thread_id is None:
            logger.error("预扣失败：无法获取 thread_id")
            return

        # 检查冻结（D16/D26）
        if self._service.is_frozen(self._owner_id):
            raise InsufficientCreditsError("积分余额不足，无法开始创作。请联系管理员补充积分。")

        # 创建预扣
        hold = self._service.create_hold(
            user_id=self._owner_id, thread_id=thread_id,
            trace_id=self._trace_id, tier=tier,
        )
        if hold is None:
            raise InsufficientCreditsError("积分余额不足，无法开始创作。请联系管理员补充积分。")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_or_load_active_hold(self) -> dict | None:
        """获取当前 thread 的活跃预扣（惰性查找，避免每次 model_call 都查库）。"""
        thread_id = _extract_thread_id_from_config()
        if thread_id is None:
            return None
        return self._service.get_active_hold(thread_id)


# ════════════════════════════════════════════════════════════════
# 模块级工具函数（私有，无状态）
# ════════════════════════════════════════════════════════════════


def _extract_usage(response: Any) -> dict[str, int | None] | None:
    """从模型响应提取 token usage。

    复用 trace_middleware._usage_payload 的提取逻辑（兼容多种格式）。
    """
    try:
        from app.platform.agent.middleware.trace_middleware import _usage_payload
        return _usage_payload(response)
    except Exception:
        return None


def _extract_cached_tokens(response: Any) -> int:
    """尝试从响应提取缓存命中的 token 数（deepseek prompt caching）。

    deepseek/OpenAI 的 usage 里可能有 prompt_cache_hit_tokens / cached_tokens 字段。
    找不到返回 0（保守：把缓存命中当未命中计费，对平台有利）。
    """
    try:
        result = getattr(response, "result", response)
        # 递归搜索 usage 字典中的缓存命中 token
        usage_raw = _search_for_usage_dict(result) or _search_for_usage_dict(response)
        if usage_raw is None:
            return 0
        for key in ("prompt_cache_hit_tokens", "cached_tokens", "prompt_tokens_details"):
            val = usage_raw.get(key)
            if isinstance(val, dict):
                val = val.get("cached_tokens")
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
        return 0
    except Exception:
        return 0


def _search_for_usage_dict(value: Any) -> dict | None:
    """递归搜索值中的 usage/token_usage 字典。"""
    if isinstance(value, dict):
        for key in ("usage", "token_usage", "usage_metadata"):
            v = value.get(key)
            if isinstance(v, dict):
                return v
        # 搜 response_metadata
        rm = value.get("response_metadata")
        if isinstance(rm, dict):
            for key in ("token_usage", "usage"):
                v = rm.get(key)
                if isinstance(v, dict):
                    return v
    return None


def _extract_subagent_type(args: Any) -> str | None:
    """从 task 工具的 args 提取 subagent_type。"""
    if isinstance(args, dict):
        return args.get("subagent_type") or args.get("name")
    if hasattr(args, "subagent_type"):
        return args.subagent_type
    if hasattr(args, "name"):
        return args.name
    return None


def _extract_thread_id_from_config() -> str | None:
    """从 LangGraph 配置提取 thread_id。"""
    try:
        from langgraph.config import get_config
        config = get_config()
        return config.get("configurable", {}).get("thread_id")
    except Exception:
        return None
