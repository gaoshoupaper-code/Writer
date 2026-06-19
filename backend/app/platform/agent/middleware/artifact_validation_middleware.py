"""ArtifactValidationMiddleware — 产物文件校验中间件。

职责：
  在代理尝试输出最终回答时（即 AI 消息无工具调用），检查期望的产物文件
  是否已存在且非空。如果产物缺失或为空，拦截输出并提醒代理先完成写入。

设计思路：
  参照 GoalMiddleware 的 after_model hook 模式：
  - 代理还在工具循环中（AI 消息含 tool_calls）→ 放行
  - 代理尝试最终回答但产物缺失 → 拦截，跳回 model 重新生成
  - 产物齐全 → 放行
  - 连续拦截超过 3 次 → 强制输出并标记警告

与原 pipeline validate 节点的区别：
  - validate 节点在特定 pipeline 阶段执行，失败则抛异常终止管道。
  - 本中间件通过 after_model hook 拦截，给代理自愈机会。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, ContextT, ResponseT, hook_config
from langchain_core.messages import AIMessage, RemoveMessage
from langgraph.runtime import Runtime
from typing_extensions import override

# 最大连续拦截次数
_MAX_VALIDATION_BLOCKS = 3
# 拦截达到上限时的强制警告
_VALIDATION_FAILURE_TEXT = (
    "Artifact validation failed: the model attempted to produce a final answer 3 consecutive times "
    "without writing the required artifact files. Accepting current output."
)


class ArtifactValidationMiddleware(AgentMiddleware):
    """产物文件校验中间件。

    通过 after_model hook 在代理输出最终回答前检查产物文件。
    缺失时拦截输出，提醒代理完成写入。
    """

    def __init__(self, artifact_paths: list[Path]) -> None:
        """
        Args:
            artifact_paths: 期望的产物文件/目录路径列表。
                            路径为目录时，检查目录下是否有非空的 .md 文件。
                            路径为文件时，检查文件存在且非空。
        """
        self.artifact_paths = artifact_paths

    def _check_artifacts(self) -> list[str]:
        """检查所有产物文件，返回缺失或为空的产物描述列表。"""
        missing = []
        for path in self.artifact_paths:
            if path.is_dir():
                has_content = any(
                    child.is_file()
                    and child.suffix == ".md"
                    and child.read_text(encoding="utf-8").strip()
                    for child in path.iterdir()
                )
                if not has_content:
                    missing.append(f"{path.name}/ 下没有非空的 Markdown 文件")
            elif not path.exists():
                missing.append(f"{path.name} 不存在")
            elif not path.read_text(encoding="utf-8").strip():
                missing.append(f"{path.name} 为空")
        return missing

    # ------------------------------------------------------------------
    # after_model hook：模型输出后的产物完整性检查
    # ------------------------------------------------------------------

    @hook_config(can_jump_to=["model"])
    @override
    def after_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """同步：模型输出后的产物检查。"""
        return _guard_artifact_completion(state, self.artifact_paths, self._check_artifacts)

    @hook_config(can_jump_to=["model"])
    @override
    async def aafter_model(self, state: Any, runtime: Runtime) -> dict[str, Any] | None:
        """异步：模型输出后的产物检查。"""
        return _guard_artifact_completion(state, self.artifact_paths, self._check_artifacts)


def _guard_artifact_completion(
    state: Any,
    artifact_paths: list[Path],
    check_fn: Callable[[], list[str]],
) -> dict[str, Any] | None:
    """核心拦截逻辑：阻止代理在产物未完成时输出最终回答。

    检查流程：
    1. 找到最后一条 AI 消息
    2. 如果 AI 消息包含工具调用，说明还在工具循环中，放行
    3. 检查所有期望的产物文件是否存在且非空
    4. 如果产物齐全，放行
    5. 否则拦截：移除 AI 消息 + 递增拦截计数 + 注入提醒 + 跳回模型
    6. 连续拦截超过上限时，报告警告并终止
    """
    # 没有期望的产物路径，直接放行
    if not artifact_paths:
        return None

    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return None

    # 找到最后一条 AI 消息
    last_ai_msg = None
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            last_ai_msg = msg
            break
        if isinstance(msg, dict) and msg.get("type") == "ai":
            last_ai_msg = msg
            break

    if not last_ai_msg:
        return None

    # AI 消息含工具调用 → 还在工具循环中，放行
    tool_calls = getattr(last_ai_msg, "tool_calls", None) or (
        last_ai_msg.get("tool_calls") if isinstance(last_ai_msg, dict) else None
    )
    if tool_calls:
        return None

    # 检查产物
    missing = check_fn()
    if not missing:
        return None

    # 产物缺失 → 拦截
    block_count = _get_block_count(state) + 1
    blocked_message = RemoveMessage(id=last_ai_msg.id) if getattr(last_ai_msg, "id", None) else None

    if block_count >= _MAX_VALIDATION_BLOCKS:
        return {
            "artifact_validation_blocked": True,
            "artifact_validation_block_count": block_count,
            "messages": [
                msg for msg in [
                    blocked_message,
                    AIMessage(content=_VALIDATION_FAILURE_TEXT),
                ] if msg
            ],
        }

    missing_desc = "；".join(missing)
    return {
        "jump_to": "model",
        "artifact_validation_blocked": True,
        "artifact_validation_block_count": block_count,
        "messages": [
            msg for msg in [
                blocked_message,
                AIMessage(content=(
                    f"产物校验未通过，以下文件尚未写入或为空：{missing_desc}。"
                    "请先完成这些文件的写入，再回复父代理。"
                )),
            ] if msg
        ],
    }


def _get_block_count(state: Any) -> int:
    """获取当前的拦截计数。"""
    if isinstance(state, dict):
        return int(state.get("artifact_validation_block_count") or 0)
    return int(getattr(state, "artifact_validation_block_count", 0) or 0)
