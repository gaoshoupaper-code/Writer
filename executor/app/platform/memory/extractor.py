"""NWM 记忆抽取器——把章节正文转成 8 类 typed records。

论文 §3.1 Publish：extractor 读 accepted prose，emit typed records（不是摘要），
每条带 source_chapter + evidence span。records merge into cumulative registries。

实现（D-D2-3 单次 LLM 多输出）：
  每章一次 LLM 调用，一次性抽取该章包含的所有 typed records（结构化 JSON）。
  用 json_object 降级 + schema hint（DeepSeek/GLM 不支持 json_schema），
  复用 client.py 验证过的 _build_schema_hint 逻辑。

模型（D-D2-3）：独立配置 MEMORY_EXTRACT_MODEL（deepseek-chat/glm-4.6，非思考模式），
  不走 ctx.model（写作用的 DeepSeek 思考模式，抽取结构化任务不需要思考）。

失败语义：LLM 调用失败 / JSON 解析失败 / schema 校验失败 → 抛 MemoryExtractError，
  由 ingestion 捕获后标记 .memory_unhealthy（D-R5-1 降级全量注入）。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# 抽取输出 Schema（与 store.py 的 _RECORD_TYPES 字段对齐）
# ════════════════════════════════════════════════════════════════════
# 设计原则：
#   - 每条 record 带 evidence_span（论文 evidence-backed，LLM 从原文摘引用句）
#   - list/dict 字段在 schema 里标注清楚（schema_hint 会展开类型说明）
#   - 字段名与 store._RECORD_TYPES 的语义字段一一对应（ingestion 直接转存）


class ChapterDigestRecord(BaseModel):
    summary: str = Field(description="本章事件/状态变化/场景骨架摘要，2-4句")
    key_events: list[str] = Field(default_factory=list, description="本章关键事件列表（如 ['E005 迷药控制','E006 初次羞辱']）")


class SceneRecord(BaseModel):
    scene_id: str = Field(description="场景标识，如 'ch7-scene1'")
    location: str = Field(default="", description="场景地点")
    participants: list[str] = Field(default_factory=list, description="参与角色名列表")
    event_order: int = Field(default=0, description="故事内时间顺序（小=早）")
    reveal_order: int = Field(default=0, description="揭露顺序：读者何时知道此事（可能与event_order不同）")
    summary: str = Field(default="", description="场景简述")
    evidence_span: str = Field(description="支撑此场景的原文引用句")


class CharacterStateRecord(BaseModel):
    name: str = Field(description="角色名（与character/下文件一致）")
    goal: str = Field(default="", description="本章展现的当前目标")
    knowledge: list[str] = Field(default_factory=list, description="本章新增/确认的已知信息")
    unknowns: list[str] = Field(default_factory=list, description="本章确认角色尚不知道的信息（信息差）")
    status: str = Field(default="", description="本章末的状态（潜逃/受伤/被捕等）")
    location: str = Field(default="", description="本章末所在位置")
    relationship_deltas: list[str] = Field(default_factory=list, description="本章关系变化，如 ['与赵默关系恶化']")
    evidence_span: str = Field(description="支撑此状态的原文引用句")


class RelationshipStateRecord(BaseModel):
    char_a: str = Field(description="角色A名")
    char_b: str = Field(description="角色B名")
    relation_type: str = Field(default="", description="关系类型（师徒/宿敌/盟友/恋人等）")
    polarity: str = Field(default="", description="关系极性：正面/负面/中性/矛盾")
    relationship_desc: str = Field(default="", description="关系简述")
    evidence_span: str = Field(description="支撑此关系的原文引用句")


class ObjectStateRecord(BaseModel):
    """关键物品状态（视题材启用，言情类可空）。"""
    name: str = Field(description="物品名")
    owner: str = Field(default="", description="持有者")
    location: str = Field(default="", description="所在位置")
    condition: str = Field(default="", description="状态（完好/损坏/遗失等）")
    evidence_span: str = Field(description="支撑此物品状态的原文引用句")


class PlotPromiseRecord(BaseModel):
    """伏笔/承诺。论文 §3.2 的 open/closed 状态机是 NWM 碾压点。"""
    promise_id: str = Field(description="伏笔唯一标识（语义ID，如 '复仇之约'/'玉佩秘密'，跨章节同名表示同一伏笔）")
    thread_id: str = Field(default="", description="所属故事线")
    structural_role: str = Field(default="", description="结构角色（主线伏笔/支线悬念/人物弧光钩子等）")
    status: str = Field(description="本章后状态：open（铺设/未兑现）或 closed（本章兑现）或 updated（推进但未兑现）")
    setup_chapter_hint: int = Field(default=0, description="若是本章新铺设，填本章章节号；若是兑现旧伏笔，填0")
    promised_payoff: str = Field(default="", description="承诺的兑现内容（铺设时填）")
    resolution: str = Field(default="", description="实际兑现描述（兑现时填）")
    evidence_span: str = Field(description="支撑此伏笔的原文引用句")


class NarrativeFunctionRecord(BaseModel):
    """叙事功能。论文 §3.2 的 focalization/reveal/dramatic function 是碾压点。"""
    scene_ref: str = Field(default="", description="关联场景ID")
    focalized_observer: str = Field(default="", description="视角人物（此场景通过谁的感知呈现）")
    dramatic_beat: str = Field(default="", description="戏剧拍子（铺垫/升级/高潮/回落/解决）")
    turn_or_reversal: str = Field(default="", description="本章转折/反转（无则空）")
    reader_knowledge: str = Field(default="", description="读者此刻知道什么（vs角色知道什么，dramatic irony 关键）")
    summary: str = Field(default="", description="叙事功能简述")
    evidence_span: str = Field(description="支撑此功能的原文引用句")


class WorldFactRecord(BaseModel):
    """世界设定。"""
    fact: str = Field(description="世界规则/设定陈述")
    category: str = Field(default="", description="类别（势力/技术/魔法体系/社会结构等）")
    scope: str = Field(default="", description="适用范围")
    evidence_span: str = Field(description="支撑此设定的原文引用句")


class ChapterRecords(BaseModel):
    """一章的全部 typed records（LLM 单次抽取的输出根 schema）。

    论文 §3.2：每类 record 独立累积进 registry，取最新有效切片。
    空列表合法（本章无此类要素）。
    """
    chapter_digest: ChapterDigestRecord = Field(description="本章摘要（必填，每章一条）")
    scenes: list[SceneRecord] = Field(default_factory=list, description="本章场景列表")
    characters: list[CharacterStateRecord] = Field(default_factory=list, description="本章角色状态变化列表")
    relationships: list[RelationshipStateRecord] = Field(default_factory=list, description="本章关系变化列表")
    objects: list[ObjectStateRecord] = Field(default_factory=list, description="本章关键物品状态（视题材）")
    promises: list[PlotPromiseRecord] = Field(default_factory=list, description="本章伏笔/承诺（铺设或兑现）")
    narrative_functions: list[NarrativeFunctionRecord] = Field(default_factory=list, description="本章叙事功能")
    world_facts: list[WorldFactRecord] = Field(default_factory=list, description="本章揭示的世界设定")


# ════════════════════════════════════════════════════════════════════
# schema_hint（从 client.py 复用，改造为纯函数）
# ════════════════════════════════════════════════════════════════════

def _type_label(ftype: Any) -> str:
    """递归标注字段类型（中文描述，引导 LLM 输出正确类型）。"""
    origin = get_origin(ftype)
    if origin is list:
        inner = get_args(ftype)[0]
        return f"列表，每个元素是{_type_label(inner)}"
    if isinstance(ftype, type) and issubclass(ftype, BaseModel):
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


def _make_example(ftype: Any, depth: int = 0) -> Any:
    """递归生成 JSON 示例骨架（让 LLM 看到期望的结构）。"""
    if depth > 6:
        return "<值>"
    origin = get_origin(ftype)
    if origin is list:
        inner = get_args(ftype)[0]
        return [_make_example(inner, depth + 1)]
    if isinstance(ftype, type) and issubclass(ftype, BaseModel):
        return {n: _make_example(f.annotation, depth + 1) for n, f in ftype.model_fields.items()}
    if ftype is str:
        return "示例文本"
    if ftype is int:
        return 0
    if ftype is float:
        return 0.0
    if ftype is bool:
        return False
    return "<值>"


def build_schema_hint(model_cls: type[BaseModel]) -> str:
    """从 pydantic model 生成 schema 描述，注入 system message 引导 LLM 输出 JSON。

    复杂嵌套（list[BaseModel]）递归展开，否则模型会猜错结构。
    逻辑源自 client.py 验证过的 _build_schema_hint，改为接受任意 model。
    """
    field_descs: list[str] = []
    skeleton: dict[str, Any] = {}
    for fname, finfo in model_cls.model_fields.items():
        tlabel = _type_label(finfo.annotation)
        desc = finfo.description or ""
        prefix = f"  - {fname}（{tlabel}）"
        field_descs.append(f"{prefix}：{desc}" if desc else prefix)
        skeleton[fname] = _make_example(finfo.annotation)

    return (
        "\n\n【输出格式要求】请严格输出一个 JSON 对象，只输出 JSON，不要任何解释文字、不要 markdown 代码块标记。\n"
        "必须包含以下字段（直接作为顶层字段，不要嵌套在 properties 里）：\n"
        + "\n".join(field_descs)
        + "\n\n⚠️ 严格遵守类型：整数字段必须填数字；列表字段填数组（无则 []）；"
        "对象字段填嵌套对象。evidence_span 必须是从原文摘录的真实句子。"
        + "\n\n输出结构示例（根据实际章节内容填写，保持结构一致）：\n"
        + json.dumps(skeleton, ensure_ascii=False, indent=2)
    )


# ════════════════════════════════════════════════════════════════════
# 抽取 prompt（默认版，Phase 5 迁 harness 可进化）
# ════════════════════════════════════════════════════════════════════

_DEFAULT_EXTRACT_PROMPT = """你是叙事世界模型（NWM）的记忆抽取器。任务：读一章小说正文，抽取结构化的叙事状态记录。

抽取原则（NWM 论文核心）：
1. 只抽取"本章已确立的事实"，不推测未来。每条记录必须能从原文找到支撑（evidence_span 填原文引用句）。
2. 角色知识（knowledge/unknowns）是信息差追踪的关键：记录角色"现在知道什么"和"还不知道什么"。
3. 伏笔（promises）的 status：本章新铺设=open（setup_chapter_hint填本章号）；本章兑现了旧伏笔=closed（resolution填兑现描述）；本章推进但未兑现=updated。
4. promise_id 跨章节稳定：同一个伏笔（如"复仇之约"）在所有章节用同一个 promise_id，这样系统才能追踪它从 open 到 closed。
5. 叙事功能（narrative_functions）的 focalized_observer 记录视角人物（此场景通过谁的感知呈现），reader_knowledge 记录读者此刻知道什么——这是 dramatic irony（戏剧性反讽）的追踪基础。
6. reveal_order 与 event_order 可能不同：读者在第7章才知道第3章发生的事，则该事件 event_order=3、reveal_order=7。
7. 空列表合法：本章无此类要素就填 []，不要硬凑。"""


# ════════════════════════════════════════════════════════════════════
# MemoryExtractor
# ════════════════════════════════════════════════════════════════════


class MemoryExtractError(RuntimeError):
    """抽取失败（LLM 调用 / JSON 解析 / schema 校验）。"""


class MemoryExtractor:
    """章节正文 → typed records 的 LLM 抽取器。

    生命周期：进程单例（get_memory_extractor 懒加载）。
    配置：MEMORY_EXTRACT_API_KEY/BASE_URL/MODEL（回退到全局 OpenAI 配置）。
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        if not api_key:
            raise MemoryExtractError("MEMORY_EXTRACT_API_KEY 未设置，无法初始化抽取器")
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._client: AsyncOpenAI | None = None

    def _ensure_client(self) -> "AsyncOpenAI":
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url or None,
                timeout=120.0,  # 抽取一章可能较久
            )
        return self._client

    async def extract(
        self,
        chapter_text: str,
        chapter_index: int,
        *,
        system_prompt: str | None = None,
    ) -> ChapterRecords:
        """抽取一章的 typed records。

        Args:
            chapter_text: 章节正文（markdown，含标题）。
            chapter_index: 章节号（注入 prompt 让 LLM 知道在抽第几章）。
            system_prompt: 自定义抽取 prompt（harness 可进化覆盖，None 用默认）。

        Returns:
            ChapterRecords：本章全部 8 类 typed records。

        Raises:
            MemoryExtractError: LLM/JSON/schema 任一失败。
        """
        if not chapter_text.strip():
            raise MemoryExtractError(f"chapter-{chapter_index} 正文为空，无法抽取")

        prompt = system_prompt or _DEFAULT_EXTRACT_PROMPT
        # schema_hint 注入 system message（DeepSeek/GLM 不支持 json_schema 的降级方案）
        schema_hint = build_schema_hint(ChapterRecords)
        system_with_hint = prompt + schema_hint

        user_msg = (
            f"请抽取第 {chapter_index} 章的叙事状态记录。\n\n"
            f"【章节正文】\n{chapter_text}\n\n"
            f"请严格按 JSON schema 输出，evidence_span 必须是原文真实引用。"
        )

        client = self._ensure_client()
        try:
            resp = await client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_with_hint},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.1,  # 抽取要稳定低随机
                response_format={"type": "json_object"},
            )
        except Exception as e:
            raise MemoryExtractError(f"抽取 LLM 调用失败（chapter-{chapter_index}）：{e}") from e

        content = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise MemoryExtractError(
                f"抽取输出非合法 JSON（chapter-{chapter_index}）：{e}。原始前 300 字：{content[:300]}"
            ) from e

        try:
            records = ChapterRecords.model_validate(data)
        except Exception as e:
            raise MemoryExtractError(
                f"抽取输出不符合 schema（chapter-{chapter_index}）：{e}。原始前 300 字：{content[:300]}"
            ) from e

        logger.info(
            "抽取完成 chapter-%d：scenes=%d chars=%d rels=%d promises=%d narr=%d world=%d objs=%d",
            chapter_index,
            len(records.scenes), len(records.characters), len(records.relationships),
            len(records.promises), len(records.narrative_functions),
            len(records.world_facts), len(records.objects),
        )
        return records


# ── 进程单例 ────────────────────────────────────────────────────────

_extractor: MemoryExtractor | None = None
_init_attempted = False


def get_memory_extractor() -> MemoryExtractor | None:
    """获取抽取器单例（懒加载）。配置缺失返回 None（降级）。"""
    global _extractor, _init_attempted
    if _extractor is not None or _init_attempted:
        return _extractor

    _init_attempted = True
    from app.platform.core.settings import get_settings

    s = get_settings()
    # 优先记忆专用配置，回退全局 OpenAI（兼容无独立配置的部署）
    api_key = s.memory_extract_api_key or s.openai_api_key
    base_url = s.memory_extract_base_url or s.openai_base_url
    # 抽取模型默认回退到 writer_model（但不建议用思考模式模型）
    model = s.memory_extract_model or s.writer_model

    if not api_key or not model:
        logger.info("记忆抽取器未配置（API key 或 model 空），记忆抽取降级")
        return None

    try:
        _extractor = MemoryExtractor(api_key=api_key, base_url=base_url, model=model)
        logger.info("记忆抽取器就绪：model=%s base_url=%s", model, base_url or "(default)")
    except MemoryExtractError as e:
        logger.warning("记忆抽取器初始化失败，降级：%s", e)
        return None
    return _extractor


def reset_memory_extractor() -> None:
    """重置单例（测试用）。"""
    global _extractor, _init_attempted
    _extractor = None
    _init_attempted = False


__all__ = [
    "ChapterRecords",
    "ChapterDigestRecord",
    "SceneRecord",
    "CharacterStateRecord",
    "RelationshipStateRecord",
    "ObjectStateRecord",
    "PlotPromiseRecord",
    "NarrativeFunctionRecord",
    "WorldFactRecord",
    "MemoryExtractor",
    "MemoryExtractError",
    "get_memory_extractor",
    "reset_memory_extractor",
    "build_schema_hint",
]
