"""记忆 embedding 客户端（NWM 向量检索专用）。

封装智谱 embedding-3 的 OpenAI 兼容调用，对上层隐藏供应商细节。
负责把文本转成 2048 维向量，供 store.py 写入 sqlite-vec 虚拟表、retriever.py 做 KNN 检索。

设计依据：设计文档 D-D1-1（智谱 embedding-3 在线 API）、D-D1-3（语义拼接）。

为什么独立于 ctx.model：
  ctx.model 是写作用的 DeepSeek 思考模式（Chat 模型），不提供 embedding。
  embedding-3 是专用向量化模型，走独立 API key（DeepSeek 根本没有 embedding 接口）。

失败语义：
  网络/key/超时 → 抛 MemoryEmbedError，由上层（ingestion/retriever）捕获后
  触发降级（D-R5-1：记忆系统不可用 → writing 回退全量注入，不中断写作）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from app.platform.core.settings import MEMORY_EMBED_DIMENSION

logger = logging.getLogger(__name__)

# 智谱 embedding-3 单次批量上限（实测安全值，避免超请求体/超时）。
# 文本条数多时由 embed 批量分片，每批最多这么多条。
_BATCH_SIZE = 32

# 单条文本截断长度（字符）。embedding-3 上下文有限，超长文本截断后嵌入。
# 记忆 record 的语义拼接文本一般远小于此值，截断仅作防御。
_MAX_CHARS_PER_TEXT = 8000


class MemoryEmbedError(RuntimeError):
    """embedding 调用失败（网络/key/供应商错误）。"""


class MemoryEmbedder:
    """智谱 embedding-3 异步客户端。

    生命周期：进程单例（get_memory_embedder 懒加载），与 MemoryBackend 同生命周期。
    线程安全：openai AsyncOpenAI 内部有连接池，支持并发。
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise MemoryEmbedError(
                "MEMORY_EMBED_API_KEY 未设置，无法初始化 embedding 客户端"
            )
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._client: AsyncOpenAI | None = None  # 懒加载

    def _ensure_client(self) -> "AsyncOpenAI":
        """懒加载 openai 异步客户端。"""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url or None,
                timeout=60.0,
            )
        return self._client

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量把文本转向量。

        Args:
            texts: 待嵌入的文本列表（已做语义拼接，见 D-D1-3）。

        Returns:
            向量列表，顺序与 texts 一一对应。每个向量长度 = MEMORY_EMBED_DIMENSION。

        Raises:
            MemoryEmbedError: 供应商返回错误 / 网络 / key 失效。
        """
        if not texts:
            return []

        # 防御性截断：超长文本截尾（embedding-3 上下文有限）
        truncated = [t[:_MAX_CHARS_PER_TEXT] for t in texts]

        client = self._ensure_client()
        all_vectors: list[list[float]] = []

        # 分批调用（供应商对单次 input 数组长度有限制）
        for start in range(0, len(truncated), _BATCH_SIZE):
            batch = truncated[start : start + _BATCH_SIZE]
            try:
                resp = await client.embeddings.create(
                    model=self._model,
                    input=batch,
                    # 显式传维度，防模型默认值变化导致与 sqlite-vec 表结构错位。
                    # embedding-3 支持的合法值：256/512/1024/2048。
                    dimensions=MEMORY_EMBED_DIMENSION,
                )
            except Exception as e:
                # openai SDK 的异常类型繁多，统一包装成 MemoryEmbedError。
                logger.error(
                    "embedding 调用失败：model=%s batch_size=%d error=%s",
                    self._model, len(batch), e,
                )
                raise MemoryEmbedError(f"embedding 调用失败：{e}") from e

            # openai 响应的 data 按 index 排序后取 embedding
            ordered = sorted(resp.data, key=lambda d: d.index)
            for item in ordered:
                vec = item.embedding
                # 维度校验：与 sqlite-vec 表 float[N] 必须一致，否则写入会错位。
                if len(vec) != MEMORY_EMBED_DIMENSION:
                    raise MemoryEmbedError(
                        f"embedding 维度异常：期望 {MEMORY_EMBED_DIMENSION}，"
                        f"实际 {len(vec)}（model={self._model}）。"
                        f"检查 MEMORY_EMBED_MODEL 是否为 embedding-3 或调整 MEMORY_EMBED_DIMENSION。"
                    )
                all_vectors.append(vec)

        logger.debug(
            "embedding 完成：model=%s count=%d dim=%d",
            self._model, len(all_vectors), MEMORY_EMBED_DIMENSION,
        )
        return all_vectors

    async def embed_one(self, text: str) -> list[float]:
        """单条文本转向量（检索 query 用）。"""
        vectors = await self.embed([text])
        return vectors[0]


# ── 进程单例 ────────────────────────────────────────────────────────

_embedder: MemoryEmbedder | None = None
_init_attempted = False


def get_memory_embedder() -> MemoryEmbedder | None:
    """获取 embedding 客户端单例（懒加载）。

    任何配置缺失（key 为空）时返回 None，调用方据此降级。
    与 get_memory_backend 解耦：backend 可能因其他原因不可用，embedder 独立判断。
    """
    global _embedder, _init_attempted
    if _embedder is not None or _init_attempted:
        return _embedder

    _init_attempted = True
    from app.platform.core.settings import get_settings

    s = get_settings()
    # 优先用记忆专用配置，回退到全局 OpenAI 配置（兼容无独立 embedding 配置的部署）。
    # 注意：DeepSeek 不提供 embedding，回退到 OpenAI 配置通常也会失败，
    # 但保留回退链让"OpenAI 原生 key"的部署能直接用 text-embedding-3-small 等。
    api_key = s.memory_embed_api_key or s.openai_api_key
    base_url = s.memory_embed_base_url or s.openai_base_url
    model = s.memory_embed_model

    if not api_key:
        logger.info("记忆 embedding 未配置（MEMORY_EMBED_API_KEY 空），向量检索降级")
        return None

    try:
        _embedder = MemoryEmbedder(api_key=api_key, base_url=base_url, model=model)
        logger.info(
            "记忆 embedding 客户端就绪：model=%s base_url=%s dim=%d",
            model, base_url or "(default)", MEMORY_EMBED_DIMENSION,
        )
    except MemoryEmbedError as e:
        logger.warning("记忆 embedding 客户端初始化失败，向量检索降级：%s", e)
        return None
    return _embedder


def reset_memory_embedder() -> None:
    """重置单例（测试用）。"""
    global _embedder, _init_attempted
    _embedder = None
    _init_attempted = False
