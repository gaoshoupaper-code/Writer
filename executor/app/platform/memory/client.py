"""Graphiti 客户端工厂（进程单例，懒加载）。

封装 Graphiti + FalkorDB + LLM + embedder 的初始化，对上层屏蔽供应商细节。
复用 spike（memory_spike/common.py）验证过的配置：
  - 抽取 LLM：DeepSeek（复用生产 OPENAI_API_KEY / OPENAI_BASE_URL / WRITER_MODEL）
  - embedding：智谱 embedding-3（1024 维 = Graphiti 默认，需独立 EMBED_API_KEY）
  - FalkorDB：Redis 协议，docker 服务名连接

为什么需要 LLM client 降级（CountingOpenAIClient 的核心逻辑，生产版保留）：
  Graphiti 默认用 OpenAI 的 Structured Outputs（response_format=json_schema）。
  DeepSeek/GLM 不支持 json_schema，只支持 json_object。
  不降级 → Graphiti 抽取阶段全部报错。

懒加载策略（与 TraceRecorder 模块级实例化的区别）：
  TraceRecorder 是纯内存组件，启动即创建不会失败。
  Graphiti 客户端依赖外部服务（FalkorDB + LLM API），启动时连不上会崩。
  因此用 get_memory_backend() 懒加载：首次调用时才创建，失败返回 None，
  让 executor 在无 FalkorDB 时也能正常启动（记忆功能自动关闭）。
"""
from __future__ import annotations

import json
import logging
import os
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from graphiti_core_falkordb import Graphiti

logger = logging.getLogger(__name__)

# 进程级单例缓存
_memory_backend: MemoryBackend | None = None
_init_attempted = False


def _make_llm_client() -> Any:
    """构建降级版 LLM client（json_schema → json_object）。

    动态创建 OpenAIClient 子类，重写 _create_structured_completion 和 _create_completion，
    把 Graphiti 默认的 response_format=json_schema 降级为 json_object。
    DeepSeek/GLM 不支持 json_schema，不降级会导致抽取阶段全部报错。

    降级逻辑来自 spike common.py，去掉 token 埋点。
    """
    from graphiti_core_falkordb.llm_client.config import LLMConfig
    from graphiti_core_falkordb.llm_client.openai_client import OpenAIClient

    model = os.environ.get("WRITER_MODEL") or os.environ.get("OPENAI_MODEL", "deepseek-chat")
    base_url = os.environ.get("OPENAI_BASE_URL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未设置，无法初始化记忆系统 LLM client")

    # small_model 必须显式设：Graphiti 有 medium/small 双模型机制（去重等轻量任务走 small）。
    # 默认 None 时用内置 gpt-4.1-nano（OpenAI 专用），DeepSeek 不支持会报错。
    small_model = os.environ.get("MEMORY_SMALL_MODEL", model)
    config = LLMConfig(api_key=api_key, model=model, small_model=small_model, base_url=base_url)

    class _ProductionOpenAIClient(OpenAIClient):

        async def _create_structured_completion(
            self,
            model: str,
            messages: list,
            temperature: float | None,
            max_tokens: int,
            response_model: type | None = None,
        ):
            """降级：json_object + schema 注入 + pydantic 校验。"""
            schema_hint = _build_schema_hint(response_model)

            injected = False
            patched = []
            for m in messages:
                if m.get("role") == "system" and not injected:
                    patched.append({**m, "content": m["content"] + schema_hint})
                    injected = True
                else:
                    patched.append(m)
            if not injected:
                patched.insert(0, {"role": "system", "content": schema_hint})

            raw = await self.client.chat.completions.create(
                model=model,
                messages=patched,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )

            content = raw.choices[0].message.content or "{}"
            try:
                data = json.loads(content)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"json_object 降级：模型输出非合法 JSON。原始前 200 字：{content[:200]}"
                ) from e

            if response_model is not None:
                try:
                    parsed = response_model.model_validate(data)
                except Exception as e:
                    raise ValueError(
                        f"json_object 降级：模型输出不符合 schema。错误：{e}。原始前 200 字：{content[:200]}"
                    ) from e
            else:
                parsed = SimpleNamespace(model_dump=lambda: data)

            fake_msg = SimpleNamespace(parsed=parsed, refusal=None)
            fake_choice = SimpleNamespace(message=fake_msg)
            return SimpleNamespace(choices=[fake_choice], usage=raw.usage)

        async def _create_completion(self, model, messages, temperature, max_tokens, response_model=None):
            """非结构化补全也统一走 json_object（DeepSeek 友好）。"""
            return await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )

    return _ProductionOpenAIClient(config=config)


def _build_schema_hint(response_model: type | None) -> str:
    """从 pydantic response_model 构造 schema 描述，注入 system message 引导输出。

    复杂嵌套类型（list[X]、嵌套 BaseModel）必须递归展开，否则模型会猜错类型。
    逻辑来自 spike common.py 的 make_example + type_label。
    """
    if response_model is None:
        return ""

    from typing import get_args, get_origin
    from pydantic import BaseModel as PydanticBaseModel

    def type_label(ftype):
        origin = get_origin(ftype)
        if origin is list:
            inner = get_args(ftype)[0]
            return f"列表，元素为{type_label(inner)}"
        if isinstance(ftype, type) and issubclass(ftype, PydanticBaseModel):
            return "对象"
        if ftype is int:
            return "整数（不能是字符串）"
        if ftype is str:
            return "字符串"
        if ftype is float:
            return "数值"
        if ftype is bool:
            return "布尔"
        return str(ftype)

    def make_example(ftype, depth=0):
        if depth > 5:
            return "<值>"
        origin = get_origin(ftype)
        if origin is list:
            inner = get_args(ftype)[0]
            return [make_example(inner, depth + 1)]
        if isinstance(ftype, type) and issubclass(ftype, PydanticBaseModel):
            nested = {}
            for nfname, nfinfo in ftype.model_fields.items():
                nested[nfname] = make_example(nfinfo.annotation, depth + 1)
            return nested
        if ftype is str:
            return "示例文本"
        if ftype is int:
            return 0
        if ftype is float:
            return 0.0
        if ftype is bool:
            return False
        return "<值>"

    field_descs = []
    skeleton = {}
    for fname, finfo in response_model.model_fields.items():
        tlabel = type_label(finfo.annotation)
        desc = finfo.description or ""
        prefix = f"  - {fname}（{tlabel}）"
        field_descs.append(f"{prefix}：{desc}" if desc else prefix)
        skeleton[fname] = make_example(finfo.annotation)

    return (
        "\n\n【输出格式要求】请严格输出一个 JSON 对象，只输出 JSON，不要任何解释文字。\n"
        "必须包含以下字段（不要嵌套在 properties 里，直接作为顶层字段）：\n"
        + "\n".join(field_descs)
        + "\n\n⚠️ 严格遵守类型：整数字段必须填数字（如 0、1、2），"
        "绝不能填 UUID 或其他字符串；没有匹配项时整数列表填空数组 []。"
        + "\n\n输出结构示例（根据实际内容填写，保持结构一致）：\n"
        + json.dumps(skeleton, ensure_ascii=False, indent=2)
    )


def _build_graphiti_client() -> "Graphiti":
    """构建 Graphiti 客户端实例（内部用，不缓存）。"""
    from graphiti_core_falkordb import Graphiti
    from graphiti_core_falkordb.driver.falkordb_driver import FalkorDriver

    # ── LLM client（降级版）──
    llm_client = _make_llm_client()

    # ── embedding（智谱 embedding-3，1024 维 = Graphiti 默认）──
    embed_api_key = os.environ.get("MEMORY_EMBED_API_KEY")
    if not embed_api_key:
        raise RuntimeError(
            "MEMORY_EMBED_API_KEY 未设置（智谱 embedding key，记忆系统必需）"
        )
    embed_base_url = os.environ.get(
        "MEMORY_EMBED_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
    )
    embed_model = os.environ.get("MEMORY_EMBED_MODEL", "embedding-3")

    from graphiti_core_falkordb.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            embedding_model=embed_model,
            api_key=embed_api_key,
            base_url=embed_base_url,
        )
    )

    # ── FalkorDB driver ──
    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", "6380"))
    driver = FalkorDriver(host=host, port=port)

    graphiti = Graphiti(graph_driver=driver, llm_client=llm_client, embedder=embedder)
    return graphiti


# 延迟 import：避免 graphiti 未安装时整个模块 import 失败
if TYPE_CHECKING:
    from app.platform.memory.backend import MemoryBackend


def get_memory_backend() -> "MemoryBackend | None":
    """获取进程级 MemoryBackend 单例（懒加载）。

    首次调用时初始化 Graphiti 客户端 + 建索引。失败返回 None（记忆功能关闭，
    executor 降级到 ContextAssembler 全量注入）。后续调用返回缓存实例。

    返回 None 的场景：
      - graphiti-core-falkordb 未安装
      - FalkorDB 连接失败
      - LLM/embedding 配置缺失
    """
    global _memory_backend, _init_attempted
    if _memory_backend is not None or _init_attempted:
        return _memory_backend
    _init_attempted = True  # 只尝试一次，失败不反复重试（避免每个请求都卡在初始化）

    try:
        from app.platform.memory.backend import MemoryBackend

        client = _build_graphiti_client()
        backend = MemoryBackend(graphiti_client=client)

        # 异步建索引——在同步上下文用 asyncio.run 不安全（FastAPI 已有 event loop）。
        # 索引建立延迟到首次 add_episode/retrieve 时（Graphiti 内部会按需建），
        # 或在 lifespan startup 中调 backend.ensure_indices()。
        _memory_backend = backend
        logger.info("记忆系统 MemoryBackend 初始化成功（FalkorDB=%s:%s）",
                     os.environ.get("FALKORDB_HOST", "localhost"),
                     os.environ.get("FALKORDB_PORT", "6380"))
        return _memory_backend

    except ImportError:
        logger.warning("graphiti-core-falkordb 未安装，记忆系统关闭，降级到全量注入")
        return None
    except Exception as e:
        logger.warning("MemoryBackend 初始化失败，记忆系统关闭：%s", e, exc_info=True)
        return None


def reset_memory_backend() -> None:
    """重置单例（仅用于测试）。"""
    global _memory_backend, _init_attempted
    _memory_backend = None
    _init_attempted = False


__all__ = ["get_memory_backend", "reset_memory_backend"]
