"""spike 共享底座：Graphiti 连接 + token 埋点 + 中文检测。

验证过的 API（graphiti-core-falkordb 0.19.10）：
  Graphiti(graph_driver=FalkorDriver(...), llm_client, embedder)
  Graphiti.add_episode(name, episode_body, source_description, reference_time,
                       source, group_id, entity_types, ...)
  LLMClient 调用链：generate_response（模板）→ _generate_response_with_retry → _generate_response（子类实现）
  继承 OpenAIClient 重写 _generate_response 包埋点（不破坏 schema 注入/clean/retry）
  依赖：graphiti-core-falkordb + falkordb（Python 客户端）

环境变量：
  SPIKE_LLM_API_KEY   LLM API key（必填）
  SPIKE_LLM_BASE_URL  OpenAI 兼容 endpoint（可选）
  SPIKE_LLM_MODEL     模型名（默认 gpt-4o-mini）
  FALKORDB_PORT       FalkorDB 端口（默认 6380）
"""
from __future__ import annotations

import os
from typing import Any

from graphiti_core_falkordb.llm_client.config import LLMConfig
from graphiti_core_falkordb.llm_client.openai_client import OpenAIClient
from graphiti_core_falkordb.prompts.models import Message


# ── token / 调用次数埋点（Spike 4 核心需求）──────────────────────

class LLMCallCounter:
    """统计 Graphiti 内部 LLM + embedding 调用次数和 token 量。"""
    def __init__(self) -> None:
        self.llm_calls = 0
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        self.llm_total_tokens = 0
        self.embed_calls = 0
        self.events: list[dict] = []

    def record_llm(self, usage: dict | None, label: str = "") -> None:
        self.llm_calls += 1
        pt = int((usage or {}).get("prompt_tokens", 0))
        ct = int((usage or {}).get("completion_tokens", 0))
        self.llm_prompt_tokens += pt
        self.llm_completion_tokens += ct
        self.llm_total_tokens += pt + ct
        self.events.append({
            "type": "llm", "label": label,
            "prompt_tokens": pt, "completion_tokens": ct,
        })

    def record_embed(self, label: str = "") -> None:
        self.embed_calls += 1
        self.events.append({"type": "embed", "label": label})

    def summary(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "embed_calls": self.embed_calls,
            "total_calls": self.llm_calls + self.embed_calls,
            "llm_prompt_tokens": self.llm_prompt_tokens,
            "llm_completion_tokens": self.llm_completion_tokens,
            "llm_total_tokens": self.llm_total_tokens,
        }

    def reset(self) -> None:
        self.llm_calls = 0
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        self.llm_total_tokens = 0
        self.embed_calls = 0
        self.events = []


COUNTER = LLMCallCounter()


# ── 带埋点的 LLM client（继承 OpenAIClient，只包一层埋点）─────────

class CountingOpenAIClient(OpenAIClient):
    """继承 OpenAIClient，重写 _generate_response 在返回前埋点 token usage。

    保留基类的 schema 注入 / clean / retry / structured completion 全部逻辑，
    只在拿到响应后从 openai response.usage 提取 token 埋点。
    """

    def __init__(self, config: LLMConfig | None = None, counter: LLMCallCounter | None = None) -> None:
        super().__init__(config=config)
        self._counter = counter or COUNTER

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type | None = None,
        max_tokens: int = 8192,
        model_size: str = "medium",
    ) -> dict[str, Any]:
        # 调基类（保留 schema 注入/retry/structured 全部逻辑）
        result = await super()._generate_response(messages, response_model, max_tokens, model_size)
        # 埋点：从结果里取 usage（OpenAIClient 的 _handle_* 把 usage 放进返回 dict）
        usage = result.get("usage") if isinstance(result, dict) else None
        label = getattr(response_model, "__name__", "freeform") if response_model else "freeform"
        self._counter.record_llm(usage, label=str(label)[:40])
        return result


# ── 构建带埋点的 Graphiti ────────────────────────────────────────

def build_graphiti(counter: LLMCallCounter | None = None) -> tuple[Any, LLMCallCounter]:
    """构建连本地 FalkorDB 的 Graphiti，LLM 走用户 key 并埋点 token。"""
    if counter is None:
        counter = COUNTER

    model = os.environ.get("SPIKE_LLM_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("SPIKE_LLM_BASE_URL")
    api_key = os.environ.get("SPIKE_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("SPIKE_LLM_API_KEY 未设置")

    port = os.environ.get("FALKORDB_PORT", "6380")

    # LLM client（继承 OpenAIClient，带埋点）
    llm_config = LLMConfig(api_key=api_key, model=model, base_url=base_url)
    llm_client = CountingOpenAIClient(config=llm_config, counter=counter)

    # embedder
    from graphiti_core_falkordb.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    embed_config = OpenAIEmbedderConfig(
        embedding_model="text-embedding-3-small",
        api_key=api_key,
        base_url=base_url if base_url else "https://api.openai.com/v1",
    )
    embedder = OpenAIEmbedder(config=embed_config)

    # FalkorDB 专用 driver（Redis 协议）
    from graphiti_core_falkordb.driver.falkordb_driver import FalkorDriver
    driver = FalkorDriver(host="localhost", port=int(port))

    from graphiti_core_falkordb import Graphiti
    graphiti = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
    )
    return graphiti, counter


# ── 中文检测（Spike 1 用）──────────────────────────────────────

def is_mostly_chinese(text: str, threshold: float = 0.9) -> tuple[bool, float]:
    """判断文本是否以中文为主。Returns (是否达标, 中文占比)。"""
    if not text:
        return False, 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    cjk_punct = sum(1 for c in text if "\u3000" <= c <= "\u303f")
    total = len(text.strip())
    if total == 0:
        return False, 0.0
    ratio = (cjk + cjk_punct) / total
    return ratio >= threshold, ratio
