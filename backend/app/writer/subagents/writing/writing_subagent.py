"""Writing 子代理 — 正文章节写作 + 审查管道。

架构概览：
  本模块实现了"写作 → 审查 → 修订"管道：
  1. primary agent（writing）：生成或修订正文章节（chapter/*.md）
  2. secondary agent（review）：审查正文章节质量（review/*.md）
  3. 修订循环：review 建议修订时，自动让 writing 重新修订（最多 3 轮）

  管道复用 outline 模块的 _build_compiled_pipeline_subagent() 通用构建器。

核心组件：
  - ContextAssemblerMiddleware: writing 子代理的上下文组装中间件（由主代理配置文件路径）
    在每个新写作轮次开始时，从文件系统读取指定文件并注入。

  - _parse_review_result(): 审查结果解析
    从 review 代理的文本输出中解析决策（通过/建议修订/必须修订）。

  - _build_revision_instruction(): 修订指令构建
    将 review 结果和修订要求组合为 writing 代理的修订指令。

与 outline 管道的差异：
  - primary_artifact 是目录（chapter/）而非单个文件
  - secondary_artifact 是目录（review/）而非单个文件
  - 上下文通过 ContextAssemblerMiddleware 注入（由主代理配置文件路径）
  - 审查代理的上下文在 secondary_context_loader 中一次性加载
"""

from pathlib import Path
from typing import NotRequired, TypedDict

from deepagents import CompiledSubAgent
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.language_models import BaseChatModel

from app.writer.middleware.context_assembler_middleware import ContextAssemblerMiddleware
from app.writer.subagents.outline.outline_subagent import (
    MiddlewareFactory,
    SecondaryDecision,
    _agent_from_subagent_spec,
    _build_compiled_pipeline_subagent,
    _messages_text,
    _required_result,
)
from app.writer.subagents.evaluation import EvaluationType, build_evaluation_subagent

# 写作子代理的系统提示词文件路径
PROMPT_PATH = Path(__file__).resolve().parent / "prompt" / "writing_system_prompt.txt"


def _append_style(system_prompt: str, style_text: str | None) -> str:
    """将写作风格文本追加到系统提示词末尾。"""
    if not style_text:
        return system_prompt
    return f"{system_prompt}\n\n---\n{style_text}\n---"


class _RunnableSubAgentSpec(TypedDict):
    """可运行的子代理规格（内部类型）。"""
    name: str
    system_prompt: str
    permissions: NotRequired[list[FilesystemPermission]]
    middleware: NotRequired[list[AgentMiddleware]]
    response_format: NotRequired[object]


def build_writing_subagent(middleware: list[AgentMiddleware] | None = None, style_text: str | None = None) -> _RunnableSubAgentSpec:
    """构建单独的 writing 子代理规格（不含审查管道）。

    权限配置：
    - 读取：允许读取所有文件（/**）
    - 写入：只允许写入 /chapter/** （正文章节）
    - 拒绝：禁止写入其他所有文件

    Args:
        middleware:  额外中间件列表（可选）
        style_text:  写作风格文本（可选，追加到系统提示词末尾）

    Returns:
        子代理规格字典
    """
    system_prompt = _append_style(PROMPT_PATH.read_text(encoding="utf-8").strip(), style_text)
    permissions = [
        FilesystemPermission(
            operations=["read"],
            paths=["/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/chapter/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

    spec = _RunnableSubAgentSpec(
        name="writing",
        system_prompt=system_prompt,
        permissions=permissions,
    )
    if middleware is not None:
        spec["middleware"] = middleware
    return spec


def build_writing_pipeline_subagent(
    workspace_root: Path,
    model: BaseChatModel,
    backend: BackendProtocol,
    middleware_factory: MiddlewareFactory,
    style_text: str | None = None,
    context_file_paths: list[str] | None = None,
) -> CompiledSubAgent:
    """构建带审查循环的 writing 管道子代理。

    管道流程：
    1. writing 代理生成/修订正文章节（chapter/下）
    2. review 代理审查正文章节质量（review/下）
    3. 如果 review 建议修订，自动让 writing 重新修订（最多 3 轮）

    上下文注入策略：
    - writing 代理通过 ContextAssemblerMiddleware 在每个新轮次注入最新上下文
    - review 代理通过 secondary_context_loader 一次性加载所有需要的文件

    Args:
        workspace_root:      工作区根目录
        model:               聊天模型
        backend:             DeepAgents 后端（文件系统）
        middleware_factory:   中间件工厂函数
        style_text:          写作风格文本（可选）
        context_file_paths:  上下文文件路径列表（相对于工作区根目录），
                             由主代理控制；新阶段时读取这些文件并注入上下文

    Returns:
        编译后的管道子代理
    """
    # 主代理控制上下文文件路径：ContextAssemblerMiddleware 根据传入的文件路径列表
    # 读取工作区文件，在新阶段时注入上下文。阶段检测（ToolMessage 判断）也由该中间件处理。
    writing_middleware = list(middleware_factory("writing-subagent"))
    writing_middleware.append(ContextAssemblerMiddleware(
        workspace_root,
        file_paths=context_file_paths or [],
        context_label="写作前置上下文",
    ))

    writing_agent = _agent_from_subagent_spec(
        build_writing_subagent(writing_middleware, style_text),
        model,
        backend,
    )
    review_agent = _agent_from_subagent_spec(
        build_evaluation_subagent(
            EvaluationType.WRITING,
            workspace_root,
            middleware_factory("review-subagent"),
            context_file_paths=["outline.md", "character/*.md", "detail/*.md", "chapter/*.md"],
        ),
        model,
        backend,
    )
    return _build_compiled_pipeline_subagent(
        name="writing",
        description=(
            "适用：需要生成、追加或修订单个正文章节时调用；不用于大纲、角色或评估。"
            "内部会完成 chapter/ 下对应章节写入或更新，随后调用 review 写入 review/ 下对应章节审查文件；"
            "若 review 要求修订，会自动让 writing 修订同一章节，最多 3 轮，主 Agent 不需要反复调用。"
            "输入上下文包含 character/（角色设计）、outline.md（大纲剧情）和 detail/（对应细纲）；"
            "writing 会积累上下文，review 每次评估不携带历史上下文。"
        ),
        workspace_root=workspace_root,
        primary_agent=writing_agent,
        secondary_agent=review_agent,
        primary_artifact="chapter/",
        secondary_artifact="review/",
        primary_label="writing",
        secondary_label="review",
        secondary_instruction=(
            "chapter/ 下对应章节文件已成功写入或更新。当前输入已直接提供 outline.md、character/ 和 chapter/ 内容；"
            "请基于原始任务中的章节目标、承接上下文、关键约束、禁改内容和期望审查结论，"
            "审查刚完成的章节并写入 review/ 目录下对应章节的 Markdown 文件。"
        ),
        # secondary_context_loader 设为 None：
        # ContextAssemblerMiddleware 已内置在 review 代理中，自动读取
        # outline.md、character/、detail/、chapter/ 并注入上下文。
        enable_revision_loop=True,
        max_revision_count=3,
        secondary_result_parser=_parse_review_result,
        revision_instruction_builder=_build_revision_instruction,
    )


# ======================================================================
# 审查结果解析
# ======================================================================


def _parse_review_result(result: str) -> SecondaryDecision:
    """从审查代理的文本输出中解析决策。

    解析格式：中文冒号分隔的键值对，如：
      - 结论：通过
      - 是否需要 writing 修订：否
      - 给 writing 的修订指令：无

    解析逻辑：
    - 结论不在预定义值中 → 接受但记录质量风险
    - 是否需要修订不明确 → 接受但记录质量风险
    - 需要修订但无修订指令 → 接受但记录质量风险
    - 需要修订且有修订指令 → 返回 "revise" 决策
    - 不需要修订但结论非"通过" → 接受但记录质量风险
    - 通过且不需要修订 → 接受
    """
    fields = _parse_protocol_fields(result)
    conclusion = fields.get("结论")
    needs_revision = fields.get("是否需要 writing 修订")
    revision_instruction = fields.get("给 writing 的修订指令")

    if conclusion not in {"通过", "建议修订", "必须修订"}:
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": "review 回复缺少可解析的结论；已接受当前版本。",
        }
    if needs_revision not in {"是", "否"}:
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": "review 回复缺少可解析的修订判断；已接受当前版本。",
        }

    if needs_revision == "是":
        if not revision_instruction or revision_instruction == "无":
            return {
                "decision": "accept",
                "revision_instruction": "",
                "quality_risk": "review 要求修订但未提供修订指令；已接受当前版本。",
            }
        return {"decision": "revise", "revision_instruction": revision_instruction}

    if conclusion != "通过":
        return {
            "decision": "accept",
            "revision_instruction": "",
            "quality_risk": f"review 结论为\"{conclusion}\"但未要求 writing 修订；已接受当前版本。",
        }
    return {"decision": "accept", "revision_instruction": ""}


def _parse_protocol_fields(text: str) -> dict[str, str]:
    """从审查结果文本中解析中文冒号分隔的键值对。

    只提取预定义的键：结论、是否需要 writing 修订、
    是否需要调整大纲或状态、最关键问题、给 writing 的修订指令、
    review/章节文件。
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "：" not in line:
            continue
        key, value = line.split("：", 1)
        key = key.strip().removeprefix("-").strip()
        if key in {
            "结论",
            "是否需要 writing 修订",
            "是否需要调整大纲或状态",
            "最关键问题",
            "给 writing 的修订指令",
            "review/章节文件",
        }:
            fields[key] = value.strip()
    return fields


def _build_revision_instruction(state: dict) -> str:
    """构建写作修订指令（修订循环中使用）。

    包含：
    - 原始章节任务
    - 上一轮 writing 的摘要
    - 上一轮 review 的结果
    - 本轮修订要求

    约束：
    - 只修订同一个 chapter/chapter-XX.md 文件
    - 不新建章节、不修改 outline/character/state_log/evaluation
    """
    return (
        "你正在基于 review 结果修订同一个正文章节。\n\n"
        "原始章节任务：\n"
        f"{_messages_text(state['messages'])}\n\n"
        "上一轮 writing 摘要：\n"
        f"{_required_result(state, 'primary_result')}\n\n"
        "上一轮 review 结果：\n"
        f"{_required_result(state, 'secondary_result')}\n\n"
        "本轮修订要求：\n"
        f"{_required_result(state, 'revision_instruction')}\n\n"
        "只修订同一个 chapter/chapter-XX.md 文件；不要新建章节，不要修改 outline.md、character/、"
        "state_log.md 或 evaluation.md。如果确实需要调整大纲或状态，只在回复中标记，不要自行修改。"
    )
