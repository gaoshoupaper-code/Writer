"""问题知识库 embedding 客户端（REQ-04 / AC-39）。

用智谱 embedding-3（2048 维，与 executor 端对齐）把问题文本转向量，供 store 写入
sqlite-vec 虚拟表、search 做向量重排。

与 evolution core/llm.py 一致：用 httpx 直调 OpenAI 兼容协议（不依赖 openai SDK），
保持 evolution 端轻量独立。

调用计数（AC-39）：每个问题组最多 1 次 embedding 调用，同一进化运行内对同一冻结问题卡
复用结果。本模块记录总调用次数供验收观察（检索方负责按 card_id 缓存复用）。

失败语义（AC-26/DEC-13）：key 缺失或调用失败 → get_embedder 返回 None，
调用方据此降级到结构化过滤 + FTS，不抛错、不阻塞进化。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import httpx

from app.core import db
from app.core.settings import settings

logger = logging.getLogger("evolution.problem_kb.embedder")

# embedding-3 维度（与 executor 端 MEMORY_EMBED_DIMENSION 对齐）
EMBED_DIMENSION = 2048
# 单次批量上限（供应商限制）
_BATCH_SIZE = 32
# 单条文本截断（防御超长）
_MAX_CHARS = 8000
# 默认 embedding 模型
_DEFAULT_MODEL = "embedding-3"


class EmbedError(RuntimeError):
    """embedding 调用失败。"""


class ProblemKbEmbedder:
    """智谱 embedding-3 同步客户端（带调用计数）。

    线程安全：httpx.Client 内部有连接池；调用计数用锁保护。
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise EmbedError("embedding api_key 未配置")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._model = model or _DEFAULT_MODEL
        self._call_count = 0
        self._lock = threading.Lock()

    @property
    def call_count(self) -> int:
        """累计 embedding 调用次数（AC-39 验收用）。"""
        with self._lock:
            return self._call_count

    @property
    def model_version(self) -> str:
        """当前模型标识（候选 match_model_version 记录用，AC-45）。"""
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量把文本转向量（同步）。失败抛 EmbedError。"""
        if not texts:
            return []
        truncated = [t[:_MAX_CHARS] for t in texts]
        url = f"{self._base_url}/embeddings" if self._base_url else "https://open.bigmodel.cn/api/paas/v4/embeddings"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        all_vecs: list[list[float]] = []
        for start in range(0, len(truncated), _BATCH_SIZE):
            batch = truncated[start : start + _BATCH_SIZE]
            payload = {
                "model": self._model,
                "input": batch,
                "dimensions": EMBED_DIMENSION,
            }
            with self._lock:
                self._call_count += 1
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
                resp.raise_for_status()
            except Exception as e:
                logger.error("embedding 调用失败 model=%s batch=%d: %s", self._model, len(batch), e)
                raise EmbedError(f"embedding 调用失败：{e}") from e
            data = resp.json().get("data") or []
            # 按 index 排序保证顺序与 input 一致
            data.sort(key=lambda d: d.get("index", 0))
            for item in data:
                vec = item.get("embedding") or []
                if len(vec) != EMBED_DIMENSION:
                    raise EmbedError(
                        f"embedding 维度异常：期望 {EMBED_DIMENSION}，实际 {len(vec)}"
                    )
                all_vecs.append(vec)
        return all_vecs

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


# ── 进程单例 ────────────────────────────────────────────────────
_embedder: ProblemKbEmbedder | None = None
_init_attempted = False
_singleton_lock = threading.Lock()


def get_embedder() -> ProblemKbEmbedder | None:
    """获取 embedding 客户端单例（懒加载）。配置缺失返回 None（触发降级，AC-26）。

    配置来源（优先级）：
      1. settings.problem_kb_embed_api_key / base_url（专用配置）
      2. llm_configs 表 evolution scope 的 api_key / base_url（复用 chat 配置）
      model 固定 settings.problem_kb_embed_model（默认 embedding-3）。
    """
    global _embedder, _init_attempted
    if _embedder is not None or _init_attempted:
        return _embedder
    with _singleton_lock:
        if _embedder is not None or _init_attempted:
            return _embedder
        _init_attempted = True
        try:
            s = settings
            api_key = s.problem_kb_embed_api_key
            base_url = s.problem_kb_embed_base_url
            model = s.problem_kb_embed_model or _DEFAULT_MODEL
            # 专用配置缺失 → 复用 evolution scope 的 llm_config
            if not api_key:
                cfg = db.LlmConfigsRepository.get_active("evolution")
                if cfg:
                    api_key, scope_base, _scope_model = cfg
                    if not base_url:
                        base_url = scope_base
            if not api_key:
                logger.info("问题知识库 embedding 未配置（key 空），向量检索降级")
                return None
            _embedder = ProblemKbEmbedder(api_key=api_key, base_url=base_url, model=model)
            logger.info(
                "问题知识库 embedding 就绪：model=%s base_url=%s dim=%d",
                model, base_url or "(default)", EMBED_DIMENSION,
            )
        except EmbedError as e:
            logger.warning("问题知识库 embedding 初始化失败，向量检索降级：%s", e)
            return None
        return _embedder


def reset_embedder() -> None:
    """重置单例（测试用）。"""
    global _embedder, _init_attempted
    with _singleton_lock:
        _embedder = None
        _init_attempted = False


__all__ = [
    "ProblemKbEmbedder", "EmbedError", "EMBED_DIMENSION",
    "get_embedder", "reset_embedder",
]
