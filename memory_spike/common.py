"""spike 共享底座：Graphiti 连接 + token 埋点 + 中文检测。

验证过的 API（graphiti-core-falkordb 0.19.10）：
  Graphiti(graph_driver=FalkorDriver(...), llm_client, embedder)
  Graphiti.add_episode(name, episode_body, source_description, reference_time,
                       source, group_id, entity_types, ...)

CountingOpenAIClient 做两件事：
  1. token 埋点（spike4 成本统计必需）
  2. json_schema → json_object 降级（让 DeepSeek/GLM 也能跑——它们不支持
     OpenAI 的 Structured Outputs/json_schema，只支持 json_object）

环境变量：
  SPIKE_LLM_API_KEY   LLM API key（必填）
  SPIKE_LLM_BASE_URL  OpenAI 兼容 endpoint（可选）
  SPIKE_LLM_MODEL     模型名（默认 gpt-4o-mini）
  FALKORDB_PORT       FalkorDB 端口（默认 6380）
  OPENAI_API_KEY      Graphiti 内部 reranker client 初始化需要（设成和 SPIKE_LLM_API_KEY 一样即可）
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
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


# ── 带埋点 + json_schema→json_object 降级的 LLM client ───────────

import json
from types import SimpleNamespace


class CountingOpenAIClient(OpenAIClient):
    """继承 OpenAIClient，做两件事：

    1. token 埋点：重写 _generate_response，从底层 openai response 真正取到 usage。
       （基类的 _handle_*_response 返回的是 model_dump()/json.loads 的纯 dict，
       不含 usage——原埋点实现取不到，spike4 的 token 统计会全 0。这里在调用
       _create_*_completion 后直接从返回对象取 usage。）

    2. json_schema → json_object 降级：重写 _create_structured_completion，
       不用 client.beta.chat.completions.parse（发 json_schema，DeepSeek/GLM 不支持），
       改用普通 chat.completions.create + response_format=json_object，
       并在 messages 里注入 schema 描述引导输出结构。
       风险：json_object 不保证严格遵守 schema，靠 prompt 引导 + pydantic 二次校验兜底。
    """

    def __init__(self, config: LLMConfig | None = None, counter: LLMCallCounter | None = None) -> None:
        super().__init__(config=config)
        self._counter = counter or COUNTER

    async def _create_structured_completion(
        self,
        model: str,
        messages: list,
        temperature: float | None,
        max_tokens: int,
        response_model: type | None = None,
    ):
        """降级实现：json_object + schema 注入 + pydantic 校验包装。

        返回一个适配基类 _handle_structured_response 的假对象：
        response.choices[0].message.parsed（pydantic 对象，有 .model_dump()）
        """
        # 1. 从 pydantic response_model 生成 schema 描述，注入到 system message
        schema_hint = ""
        if response_model is not None:
            schema = response_model.model_json_schema()
            schema_hint = (
                "\n\n【输出格式要求】请严格输出符合以下 JSON Schema 的 JSON 对象，"
                "只输出 JSON，不要任何解释文字：\n"
                + json.dumps(schema, ensure_ascii=False, indent=2)
            )
        # 在最后一条 system message 末尾追加 schema 描述
        injected = False
        patched_messages = []
        for m in messages:
            if m.get("role") == "system" and not injected:
                patched_messages.append({**m, "content": m["content"] + schema_hint})
                injected = True
            else:
                patched_messages.append(m)
        if not injected:
            # 没有 system message，作为第一条 system 插入
            patched_messages.insert(0, {"role": "system", "content": schema_hint})

        # 2. 用 json_object 模式调用（DeepSeek/GLM/OpenAI 通用支持）
        raw_response = await self.client.chat.completions.create(
            model=model,
            messages=patched_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )

        # 3. 埋点 token usage（这里能真正拿到 usage）
        usage = getattr(raw_response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }
        label = getattr(response_model, "__name__", "freeform") if response_model else "freeform"
        self._counter.record_llm(usage_dict, label=str(label)[:40])

        # 4. 解析 JSON + 用 response_model 二次校验，构造假对象返回
        content = raw_response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            # 模型没输出合法 JSON，触发上层 retry
            raise ValueError(
                f"json_object 降级模式：模型输出非合法 JSON，无法解析。原始内容前 200 字：{content[:200]}"
            ) from e

        if response_model is not None:
            try:
                parsed_obj = response_model.model_validate(data)
            except Exception as e:
                # schema 校验失败，触发上层 retry
                raise ValueError(
                    f"json_object 降级模式：模型输出不符合 schema。错误：{e}。原始内容前 200 字：{content[:200]}"
                ) from e
        else:
            parsed_obj = SimpleNamespace(model_dump=lambda: data)

        # 包装成 _handle_structured_response 期望的结构
        fake_message = SimpleNamespace(parsed=parsed_obj, refusal=None)
        fake_choice = SimpleNamespace(message=fake_message)
        return SimpleNamespace(choices=[fake_choice], usage=raw_response.usage)

    async def _create_completion(self, model, messages, temperature, max_tokens, response_model=None):
        """非结构化补全也埋点 usage。"""
        response = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        usage = getattr(response, "usage", None)
        usage_dict = None
        if usage is not None:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }
        self._counter.record_llm(usage_dict, label="freeform")
        return response

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type | None = None,
        max_tokens: int = 8192,
        model_size: str = "medium",
    ) -> dict[str, Any]:
        # 调基类：现在基类的 _create_structured_completion/_create_completion 已被重写降级
        result = await super()._generate_response(messages, response_model, max_tokens, model_size)
        return result


# ── 构建带埋点的 Graphiti ────────────────────────────────────────

# bge-small-zh-v1.5 输出 512 维向量（实测确认）。Graphiti 默认 EMBEDDING_DIM=1024，
# 必须覆盖成 512，否则向量长度不一致（embedder 产 512，embedder_pool 按 1024 处理）报错。
BGE_EMBEDDING_DIM = 512


def override_embedding_dim() -> None:
    """把 Graphiti 的 EMBEDDING_DIM 常量改成 bge 的 512。

    Graphiti 在 embedder/client.py 定义 EMBEDDING_DIM=1024（0.19.10 版本只此一处）。
    bge-small-zh 是 512 维，必须覆盖，否则向量长度不一致。
    在 build_graphiti 内、Graphiti 构造前调用。
    """
    import graphiti_core_falkordb.embedder.client as _embedder_client

    _embedder_client.EMBEDDING_DIM = BGE_EMBEDDING_DIM
    # EmbedderConfig.embedding_dim 是 frozen Field，需绕过 frozen 限制改默认值
    _embedder_client.EmbedderConfig.model_fields['embedding_dim'].default = BGE_EMBEDDING_DIM


class LocalBGEEmbedder:
    """用 sentence-transformers 本地加载 bge-small-zh-v1.5，替代 OpenAIEmbedder。

    为什么自定义：DeepSeek 没有 embedding API（404），Graphiti 强依赖 embedding
    （实体向量化是知识图谱基础）。用本地 bge 模型零成本、零依赖外部服务。

    符合 graphiti EmbedderClient 契约：实现 async create() + create_batch()。
    sentence-transformers 是同步库，用 asyncio.to_thread 包一层避免阻塞事件循环
    （Graphiti 内部大量并发 embed 调用，阻塞会拖垮整体）。
    """

    def __init__(self, model_path: str):
        import asyncio
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_path)
        self._to_thread = asyncio.to_thread
        # 记录 embed 调用次数（spike4 成本统计的一部分）
        self._call_count = 0

    async def create(
        self, input_data
    ) -> list[float]:
        """单条文本 → 512 维向量。"""
        if isinstance(input_data, str):
            texts = [input_data]
        else:
            # token id list 等非常规输入，转成可处理的文本；实际 Graphiti 传的都是 str
            texts = list(input_data) if not isinstance(input_data, (list,)) else [str(input_data)]
        emb = await self._to_thread(self._model.encode, texts[0] if len(texts) == 1 else texts)
        self._call_count += 1
        # 统一返回单条向量（截断到 BGE_EMBEDDING_DIM）
        if emb.ndim == 2:
            return emb[0][:BGE_EMBEDDING_DIM].tolist()
        return emb[:BGE_EMBEDDING_DIM].tolist()

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """批量文本 → 多条 512 维向量。"""
        embs = await self._to_thread(self._model.encode, input_data_list)
        self._call_count += len(input_data_list)
        return [e[:BGE_EMBEDDING_DIM].tolist() for e in embs]


def build_graphiti(counter: LLMCallCounter | None = None) -> tuple[Any, LLMCallCounter]:
    """构建连本地 FalkorDB 的 Graphiti。

    LLM 走 DeepSeek（抽取对话），embedding 走本地 bge-small-zh-v1.5。
    双供应商分离配置——这是 Graphiti 生产环境的推荐用法。
    """
    if counter is None:
        counter = COUNTER

    # 覆盖 EMBEDDING_DIM 为 bge 的 512 维（必须在 Graphiti 构造之前）
    override_embedding_dim()

    model = os.environ.get("SPIKE_LLM_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("SPIKE_LLM_BASE_URL")
    api_key = os.environ.get("SPIKE_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("SPIKE_LLM_API_KEY 未设置")

    port = os.environ.get("FALKORDB_PORT", "6380")
    bge_model_path = os.environ.get("BGE_MODEL_PATH", "bge-small-zh-v1.5")

    # LLM client（继承 OpenAIClient，带埋点 + json_schema 降级）
    llm_config = LLMConfig(api_key=api_key, model=model, base_url=base_url)
    llm_client = CountingOpenAIClient(config=llm_config, counter=counter)

    # embedder：本地 bge（512 维）
    embedder = LocalBGEEmbedder(model_path=bge_model_path)

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
