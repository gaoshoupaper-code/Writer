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
  抽取 LLM（DeepSeek）：
    SPIKE_LLM_API_KEY   LLM API key（必填）
    SPIKE_LLM_BASE_URL  OpenAI 兼容 endpoint
    SPIKE_LLM_MODEL     模型名
  embedding（智谱）：
    SPIKE_EMBED_API_KEY   智谱 API key（必填）
    SPIKE_EMBED_BASE_URL  默认 https://open.bigmodel.cn/api/paas/v4
    SPIKE_EMBED_MODEL     默认 embedding-3（1024 维，等于 Graphiti 默认）
  其他：
    FALKORDB_PORT  FalkorDB 端口（默认 6380）
    OPENAI_API_KEY Graphiti 内部 reranker client 初始化需要（设成和 SPIKE_LLM_API_KEY 一样即可）
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

def build_graphiti(counter: LLMCallCounter | None = None) -> tuple[Any, LLMCallCounter]:
    """构建连本地 FalkorDB 的 Graphiti。

    双供应商分离配置（Graphiti 生产推荐用法）：
      - 抽取对话 LLM：DeepSeek（SPIKE_LLM_* 环境变量）
      - embedding：智谱 embedding-3（SPIKE_EMBED_* 环境变量）

    为什么分两个供应商：DeepSeek 不提供 embedding API，Graphiti 强依赖 embedding。
    智谱 embedding-3 是 1024 维，正好等于 Graphiti 默认 EMBEDDING_DIM，无需维度覆盖。
    """
    if counter is None:
        counter = COUNTER

    # ── 抽取 LLM（DeepSeek）──
    model = os.environ.get("SPIKE_LLM_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("SPIKE_LLM_BASE_URL")
    api_key = os.environ.get("SPIKE_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("SPIKE_LLM_API_KEY 未设置")

    # small_model 必须显式设置：Graphiti 有 medium/small 双模型机制，去重等轻量任务
    # 走 small。small_model 默认 None 时 Graphiti 用内置默认 gpt-4.1-nano（OpenAI 专用），
    # DeepSeek 不支持会报 model not found。这里默认设成和主模型一样。
    small_model = os.environ.get("SPIKE_LLM_SMALL_MODEL", model)
    llm_config = LLMConfig(api_key=api_key, model=model, small_model=small_model, base_url=base_url)
    llm_client = CountingOpenAIClient(config=llm_config, counter=counter)

    # ── embedding（智谱 embedding-3）──
    embed_api_key = os.environ.get("SPIKE_EMBED_API_KEY")
    embed_base_url = os.environ.get("SPIKE_EMBED_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    embed_model = os.environ.get("SPIKE_EMBED_MODEL", "embedding-3")
    if not embed_api_key:
        raise RuntimeError("SPIKE_EMBED_API_KEY 未设置（智谱 embedding key）")

    from graphiti_core_falkordb.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    embed_config = OpenAIEmbedderConfig(
        embedding_model=embed_model,
        api_key=embed_api_key,
        base_url=embed_base_url,
    )
    embedder = OpenAIEmbedder(config=embed_config)

    # ── FalkorDB driver（Redis 协议）──
    port = os.environ.get("FALKORDB_PORT", "6380")
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
