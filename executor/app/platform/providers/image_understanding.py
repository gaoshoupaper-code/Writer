"""图像理解（视觉）能力抽象（DD8c）。

定义 ``ImageUnderstandingProvider`` Protocol。文生图的 Agent 自评（D5 第一层）
通过此接口"看图"，输出整体质量 + 提示词匹配度（D14）。

与图像生成分离（D14：生图与视觉是两个独立 API）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ImageAnalysis:
    """单张图的视觉分析结果（D14）。"""

    quality_assessment: str  # 整体质量：构图/清晰度/伪影/畸形（D14 i）
    prompt_alignment: str  # 提示词匹配度：图是否准确表达提示词意图（D14 ii）
    raw_response: str = ""  # 原始返回（调试用）


class ImageUnderstandingProvider(Protocol):
    """图像理解能力抽象接口（DD8c）。"""

    async def analyze(
        self,
        image_data: bytes,
        *,
        prompt: str,  # 分析指令（如"评估构图质量+提示词匹配度"）
    ) -> ImageAnalysis:
        """对单张图做视觉分析。"""
        ...


__all__ = ["ImageAnalysis", "ImageUnderstandingProvider"]
