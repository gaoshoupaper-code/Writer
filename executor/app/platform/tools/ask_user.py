"""ask_user 工具 — 访谈子代理向用户提问的载体（方式2 HITL）。

机制：工具内调用 langgraph ``interrupt()`` 暂停整个图；人类通过 ``Command(resume=...)``
恢复，resume 值即本工具返回值（用户回答），由访谈子代理继续消费。
多轮 interrupt 冒泡已由 P0 spike 验证（``executor/spike_t8_interrupt.py``）。

选项化：每次提问必带结构化 options（``{label, description}``），前端渲染为可点选项 +
「自定义/补充」入口，降低非网文专业用户的表达门槛。设计见
``.claude/md/20260616_150000_访谈选项化设计.md``。
"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

ASK_USER_TOOL_DESCRIPTION = """向用户提出一个带结构化选项的问题并等待用户选择/回答。

调用后执行暂停，等待人类回复；回复内容（用户选中的选项 + 可选补充文字）作为本工具返回值。
用于需求访谈阶段逐项收集创作需求，每次只问一个问题。

必填 options（1-6 项 {label, description}）：基于已收集上下文个性化生成——
已定的前序维度（如体裁）要指导后续维度选项（定了玄幻，基调选项就围绕玄幻展开），
最匹配上下文的选项排在数组首位。description 用一句话解释选项含义，帮非网文专业用户理解。
开放维度（核心创意/金手指/对标）的选项是启发性候选，用户大概率自定义或大幅修改。
multi_select 按维度判断：对标作品/禁忌红线/配角与关系网/目标受众等天然多选维度填 true，其余默认 false。
无需在 options 里加"自定义"项——前端固定提供「自定义/补充」入口兜底。"""


class AskUserOption(BaseModel):
    label: str = Field(min_length=1, max_length=12, description="选项标签，≤12 字")
    description: str = Field(
        max_length=30, description="一句话解释该选项含义，≤30 字，帮非专业用户理解"
    )


class AskUserInput(BaseModel):
    question: str = Field(min_length=1, max_length=4000, description="要问用户的问题文本")
    options: list[AskUserOption] = Field(
        min_length=1,
        max_length=6,
        description="结构化选项（必填，1-6 项）。基于已收集上下文个性化生成，最匹配的排首位",
    )
    multi_select: bool = Field(
        default=False,
        description="是否允许多选。对标/禁忌/配角/受众等天然多选维度填 true，其余 false",
    )


def ask_user(
    question: str,
    options: list[AskUserOption],
    multi_select: bool = False,
) -> str:
    # interrupt() 暂停整个图：payload 冒泡到主 agent 的 result.interrupts，
    # Command(resume=用户回答) 后，resume 值作为返回值回到子代理继续推理。
    payload: dict[str, object] = {
        "question": question,
        "options": [opt.model_dump() for opt in options],
        "multi_select": multi_select,
    }
    answer: str = interrupt(payload)
    return answer


def build_ask_user_tool() -> StructuredTool:
    return StructuredTool.from_function(
        name="ask_user",
        description=ASK_USER_TOOL_DESCRIPTION,
        func=ask_user,
        args_schema=AskUserInput,
        infer_schema=False,
    )


__all__ = [
    "ASK_USER_TOOL_DESCRIPTION",
    "AskUserOption",
    "AskUserInput",
    "ask_user",
    "build_ask_user_tool",
]
