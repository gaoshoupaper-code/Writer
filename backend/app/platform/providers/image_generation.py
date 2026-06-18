"""图像生成能力抽象（DD8c）。

定义 ``ImageGenerationProvider`` Protocol，各提供商（字节豆包/即梦/...）实现此接口。
domain 通过 ``resolve_image_provider(owner_id)`` 按用户配置取实例。

设计要点：
- 返回 ``GeneratedImage``（含二进制 + 格式 + 元数据），由调用方落盘。
- ``seed`` 参数支持双采样（D4：同提示词不同 seed 出 2 张）。
- 接口扩展性：未来 img2img 可加 ``reference_image`` 参数或新增 ``ImageEditProvider``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class GeneratedImage:
    """单张生成图的结果。"""

    image_data: bytes  # 图片二进制（落盘前）
    format: str  # "png" / "jpeg" / ...
    metadata: dict[str, Any] = field(default_factory=dict)  # seed/耗时/原始返回等


class ImageGenerationProvider(Protocol):
    """图像生成能力抽象接口（DD8c）。"""

    async def generate(
        self,
        prompt: str,
        *,
        n: int = 1,  # 生成几张（双采样 n=2）
        size: str | None = None,  # 分辨率（如 "1024x1024"）
        seed: int | None = None,  # 随机种子（双采样用不同 seed）
    ) -> list[GeneratedImage]:
        """按提示词生成 n 张图。"""
        ...


__all__ = ["GeneratedImage", "ImageGenerationProvider"]
