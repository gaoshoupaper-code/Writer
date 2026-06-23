"""字节生图 API 占位实现（D3）。

D3 需求占位：字节跳动生图模型（型号/接入通道/计费/返回格式待补）。
当前为 mock 实现，返回纯色占位图，让文生图闭环可跑通。
真实 API 接入后替换 ``generate`` 方法体即可。

实现 ImageGenerationProvider 协议（DD8c）。
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib

from app.platform.providers.image_generation import GeneratedImage


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成一张纯色 PNG（占位图，不依赖 PIL）。"""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    raw = b""
    for _y in range(height):
        raw += b"\x00" + bytes(rgb) * width
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


class BytedanceImageProvider:
    """字节生图 API（占位）。

    真实实现待 D3 外部细节补充后替换 ``generate`` 方法体。
    当前 mock：按 prompt 哈希生成确定性纯色图（不同 prompt/prompt 出不同颜色）。
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def generate(
        self,
        prompt: str,
        *,
        n: int = 1,
        size: str | None = None,
        seed: int | None = None,
    ) -> list[GeneratedImage]:
        """生成 n 张图（占位）。

        真实实现：POST 字节生图 API，按 size/seed 参数请求，下载返回的图。
        当前 mock：prompt+seed 哈希 → RGB → 纯色 PNG。
        """
        w, h = (1024, 1024)
        if size and "x" in size:
            try:
                w, h = (int(x) for x in size.split("x", 1))
            except ValueError:
                pass

        results: list[GeneratedImage] = []
        for i in range(n):
            # prompt + seed + index → 确定性颜色（同输入同输出，便于复现）
            key = f"{prompt}|{seed}|{i}".encode()
            hsh = hashlib.md5(key).digest()
            rgb = (hsh[0], hsh[1], hsh[2])
            data = _solid_png(w, h, rgb)
            results.append(GeneratedImage(
                image_data=data,
                format="png",
                metadata={"seed": seed, "index": i, "size": f"{w}x{h}", "provider": "bytedance-mock"},
            ))
        return results


__all__ = ["BytedanceImageProvider"]
