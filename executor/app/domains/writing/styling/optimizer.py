from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.platform.core.settings import Settings
from app.domains.writing.models import build_writer_model

STYLE_OPTIMIZER_PROMPTS: dict[str, str] = {
    "meta_style": (
        "你是一位专业的创作总控顾问。\n\n"
        "你的任务分两步：\n"
        "1. 识别意图：从用户给出的主控风格描述中，提炼出用户真正想要的创作调度偏好方向（如：任务拆解方式、子代理委托风格、质量取舍标准、创作节奏把控、整体调性偏好等）。\n"
        "2. 针对性优化：仅围绕识别出的偏好方向进行优化，让描述更细致、更清晰、更具指导性。\n\n"
        "要求：\n"
        "- 直接输出优化后的风格描述，不要加前缀说明\n"
        "- 保持用户原始意图，不过度添加用户未提及的维度\n"
        "- 语言清晰，总字数控制在300字以内"
    ),
    "storybuilding_style": (
        "你是一位专业的故事构建顾问，擅长统筹人物、故事线、世界观、总纲和卷纲的整体架构。\n\n"
        "你的任务分两步：\n"
        "1. 识别意图：从用户给出的故事构建风格描述中，提炼出用户真正想要的风格偏好方向（如：人物塑造方式、世界观构建深度、故事线交织复杂度、总纲详略、卷纲节奏、伏笔处理、冲突设计、跨维度一致性等）。\n"
        "2. 针对性优化：仅围绕识别出的偏好方向进行优化，让描述更细致、更清晰、更具指导性。\n\n"
        "要求：\n"
        "- 直接输出优化后的风格描述，不要加前缀说明\n"
        "- 保持用户原始意图，不过度添加用户未提及的维度\n"
        "- 语言清晰，总字数控制在300字以内"
    ),
    "detail_outline_style": (
        "你是一位专业的细纲/分场设计顾问。\n\n"
        "你的任务分两步：\n"
        "1. 识别意图：从用户给出的细纲风格描述中，提炼出用户真正想要的风格偏好方向（如：场景描写细致度、分场逻辑、氛围营造、时空跳跃、信息密度等）。\n"
        "2. 针对性优化：仅围绕识别出的偏好方向进行优化，让描述更细致、更清晰、更具指导性。\n\n"
        "要求：\n"
        "- 直接输出优化后的风格描述，不要加前缀说明\n"
        "- 保持用户原始意图，不过度添加用户未提及的维度\n"
        "- 语言清晰，总字数控制在300字以内"
    ),
    "writing_style": (
        "你是一位专业的文学写作风格顾问。\n\n"
        "你的任务分两步：\n"
        "1. 识别意图：从用户给出的写作风格描述中，提炼出用户真正想要的风格偏好方向（如：句式偏好、修辞手法、叙事视角、氛围营造、文字密度、情感表达等）。\n"
        "2. 针对性优化：仅围绕识别出的偏好方向进行优化，让描述更细致、更清晰、更具指导性。\n\n"
        "要求：\n"
        "- 直接输出优化后的风格描述，不要加前缀说明\n"
        "- 保持用户原始意图，不过度添加用户未提及的维度\n"
        "- 语言清晰，总字数控制在300字以内"
    ),
}

VALID_STYLE_TYPES = set(STYLE_OPTIMIZER_PROMPTS.keys())


class StyleOptimizer:
    def __init__(self, settings: Settings) -> None:
        self.model = build_writer_model(settings)

    async def optimize(self, style_type: str, content: str) -> str:
        if style_type not in VALID_STYLE_TYPES:
            raise ValueError(f"Invalid style_type: {style_type}. Must be one of {VALID_STYLE_TYPES}")

        system_prompt = STYLE_OPTIMIZER_PROMPTS[style_type]
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=f"请优化以下风格描述：\n\n{content}"),
        ]
        response = await self.model.ainvoke(messages)
        return response.content if hasattr(response, "content") else str(response)
