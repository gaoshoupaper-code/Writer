"""字节视觉理解 API 占位实现（D14）。

D14 需求占位：字节视觉模型（型号/接入待补）。
当前为 mock 实现，返回固定的占位分析结果。
真实 API 接入后替换 ``analyze`` 方法体。

实现 ImageUnderstandingProvider 协议（DD8c）。
"""

from __future__ import annotations

from app.platform.providers.image_understanding import ImageAnalysis


class BytedanceVisionProvider:
    """字节视觉 API（占位）。

    真实实现待 D14 外部细节补充后替换 ``analyze`` 方法体。
    当前 mock：返回占位分析（标注为 mock 结果）。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def analyze(self, image_data: bytes, *, prompt: str) -> ImageAnalysis:
        """对单张图做视觉分析（占位）。

        真实实现：POST 字节视觉 API（image_data base64 + prompt），解析返回。
        当前 mock：返回占位分析（按图像字节数给出粗略质量描述）。
        """
        size_kb = len(image_data) / 1024
        return ImageAnalysis(
            quality_assessment=(
                f"[mock] 图像大小 {size_kb:.1f}KB。构图与清晰度待真实视觉模型评估。"
            ),
            prompt_alignment=(
                f"[mock] 与提示词「{prompt[:40]}」的匹配度待真实视觉模型评估。"
            ),
            raw_response="[mock] bytedance vision provider placeholder",
        )


__all__ = ["BytedanceVisionProvider"]
