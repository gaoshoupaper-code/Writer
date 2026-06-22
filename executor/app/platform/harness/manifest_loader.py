"""harness manifest loader（Phase 6 T4.1/T4.2，执行端核心）。

从 evolution 拉 production manifest → 解析成 AssembledManifest（装配意图）→
执行端读取后补全 model/backend/checkpointer 再调 create_deep_agent。

三层职责（严格分层，方案 Y 决策 D10）：
  - 数据层（本模块）：manifest JSON → AssembledManifest。加载 C 类代码、实例化
    B 类参数化 middleware、解析 A 类文本。**不调 create_deep_agent**（那需要
    model/backend/checkpointer，归执行端）。
  - 装配层（meta/agent.py 的 _assemble_via_manifest，T4.4）：读 AssembledManifest
    → 补基础设施 → 调 create_deep_agent。

为什么不直接调 create_deep_agent：
  model（多用户隔离）、backend（workspace 相关）、checkpointer（分库 saver）、
  TraceMiddleware（按 trace_id 插入）、evolution_spec（执行端建）——这些都由
  执行端持有，manifest 不承载（调研结论 + 设计 D11）。

C 类 surface 加载（D11 进程启动时加载）：
  worker/main 启动时调 preload_c_surfaces，importlib 加载所有 C 类 middleware
  代码片段，实例化并收集 state_schema。换 C 类版本需重启 worker。

降级链（复用 prompt loader 模式）：
  evolution HTTP 优先 → 本地缓存 → 报错。evolution 不可用不影响 agent 运行。

设计依据：设计文档 D10（方案 Y）+ D11（进程启动加载）+ D5（manifest 统一接管）。
"""
from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("writer.manifest_loader")


# ── 数据结构：AssembledManifest（装配意图）──────────────────────


@dataclass
class AssembledSubagent:
    """单个子代理的装配意图（执行端读它装配）。"""

    kind: str               # "general_purpose" / "custom" / "deep"
    name: str               # interview / storybuilding / detail-outline / writing / general-purpose
    # deep 子代理字段
    description: str = ""
    system_prompt: str = ""
    subagent_middleware: list[Any] = field(default_factory=list)  # 已实例化的 AgentMiddleware
    skills: list[str] = field(default_factory=list)              # 绝对路径
    artifact_paths: list[str] = field(default_factory=list)      # 相对 workspace 的产物路径
    max_revisions: int = 1
    evaluator_kind: str = ""                                     # storybuilding/detail-outline/writing
    # general_purpose 无额外字段；custom(interview) 无 deep 字段


@dataclass
class AssembledManifest:
    """一份 manifest 解析后的完整装配意图（执行端装配层消费）。"""

    manifest_version: int
    meta_system_prompt: str
    meta_skills: list[str]                    # 绝对路径
    meta_tools: list[Any]                     # meta 层工具（默认空）
    meta_middleware_base: list[Any]           # 已实例化的 meta middleware（不含 Trace，执行端插）
    meta_prompt_version: int | None = None    # 写进 trace
    subagents: list[AssembledSubagent] = field(default_factory=list)
    state_schemas: list[type] = field(default_factory=list)  # 所有 C 类 middleware 贡献的 state_schema
    manifest_meta: dict[str, Any] = field(default_factory=dict)  # 版本信息写 trace


# ── C 类 surface 预加载（D11 进程启动时）──────────────────────


class CSurfaceLoadError(Exception):
    """C 类 surface 代码片段加载失败。"""


# 进程级缓存：C 类 middleware 实例池（surface_name, scope) → {instance, state_schema}
# 由 preload_c_surfaces 填充，assemble 时复用（D11：随进程生命周期，换版本重启）
_c_surface_cache: dict[tuple[str, str], dict[str, Any]] = {}


def preload_c_surfaces(manifest_entries: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """加载 manifest 中所有 C 类 surface 代码片段（进程启动时调一次，D11）。

    importlib 加载每个 C 类 middleware 代码 → 找 AgentMiddleware 子类 → 实例化
    → 收集 state_schema。结果存进程级缓存供 assemble 复用。

    Args:
        manifest_entries: manifest 的 entries_json 解析结果（含 surfaces + schema_lock）

    Returns: 实例池 {(surface_name, scope): {instance, state_schema, code}}

    换 C 类版本需重启进程（D11 设计取舍：C 类是 schema 改动，重启可接受）。
    """
    global _c_surface_cache
    _c_surface_cache = {}  # 重置（重启语义）
    for entry in manifest_entries.get("surfaces", []):
        if entry["surface_type"] != "stateful_middleware":
            continue
        key = (entry["surface_name"], entry["scope"])
        try:
            instance, state_schema = _load_c_middleware(
                entry["surface_name"], entry["scope"], entry["version"],
                entry.get("_content", ""),  # 执行端 fetch 时填充的 content
            )
            _c_surface_cache[key] = {
                "instance": instance,
                "state_schema": state_schema,
            }
            logger.info(
                "预加载 C 类 surface %s/%s v%s（state_schema=%s）",
                entry["surface_name"], entry["scope"], entry["version"],
                getattr(state_schema, "__name__", state_schema),
            )
        except Exception as exc:
            logger.exception("加载 C 类 surface %s 失败", key)
            raise CSurfaceLoadError(f"C 类 surface {key} 加载失败: {exc}") from exc
    return _c_surface_cache


def _load_c_middleware(
    surface_name: str, scope: str, version: int, code: str,
) -> tuple[Any, type]:
    """importlib 加载 C 类代码片段 → 实例化 middleware → 取 state_schema。

    code 是完整的 middleware 类定义（含 import）。在执行端环境加载，
    能解析 app.domains... 等依赖（D11 前提：执行端有完整依赖）。
    """
    if not code:
        raise CSurfaceLoadError(f"C 类 surface {surface_name}/{scope} v{version} 内容为空")
    # 用唯一模块名加载（避免冲突）
    mod_name = f"_c_surface_{surface_name}_{scope}_v{version}"
    spec = importlib.util.spec_from_loader(mod_name, loader=None)
    if spec is None:
        raise CSurfaceLoadError(f"无法创建模块 spec: {mod_name}")
    module = importlib.util.module_from_spec(spec)
    # 手动 exec（spec_from_loader 无文件路径）
    exec(compile(code, f"<{mod_name}>", "exec"), module.__dict__)
    # 找 AgentMiddleware 子类
    from langchain.agents.middleware.types import AgentMiddleware
    mw_cls = None
    for attr_name, attr_val in vars(module).items():
        if (isinstance(attr_val, type)
                and attr_val.__name__ != "AgentMiddleware"
                and _issubclass_safe(attr_val, AgentMiddleware)):
            mw_cls = attr_val
            break
    if mw_cls is None:
        raise CSurfaceLoadError(
            f"C 类代码未定义 AgentMiddleware 子类: {surface_name}/{scope}"
        )
    state_schema = getattr(mw_cls, "state_schema", None)
    # state_schema 可能为基类默认值（AgentMiddleware 自带 _DefaultAgentState）。
    # 这里不强校验"子类显式声明 state_schema"——那是 evolution static_check 的职责
    # （源码级 AST 检查）。执行端只验证"能加载 + 是 middleware 子类 + 能实例化"。
    # state_schema 无论是子类显式还是继承默认，都如实返回供执行端聚合 State。
    instance = mw_cls()
    return instance, state_schema


def _issubclass_safe(cls: type, base: type) -> bool:
    try:
        return issubclass(cls, base)
    except TypeError:
        return False


# ── manifest 拉取（HTTP 优先 + 缓存降级）──────────────────────


class ManifestLoader:
    """manifest 拉取器（远程优先 + 缓存降级 + stale 机制）。

    复用 prompt loader 的降级模式（设计 D5）。
    """

    def __init__(self, evolution_url: str = "", cache_dir: str = ".manifest_cache") -> None:
        self._evolution_url = evolution_url.rstrip("/")
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._stale: bool = False  # evolution 通知后标记，下次 fetch 强制重拉

    def mark_stale(self) -> None:
        """标记缓存 stale（/internal/manifest/refreshed 端点调用）。"""
        self._stale = True

    def fetch_production(self) -> dict[str, Any] | None:
        """拉当前 production manifest（含各 surface 的 content 填充）。

        降级链：evolution HTTP → 本地缓存 → None。
        evolution 不可用时用缓存降级（保证可用性）。
        """
        if self._evolution_url:
            manifest = self._fetch_from_evolution()
            if manifest is not None:
                self._save_cache(manifest)
                self._stale = False
                return manifest
        # 降级：读缓存
        cached = self._load_cache()
        if cached is not None:
            if self._stale:
                logger.warning("manifest stale 但 evolution 不可用，用旧缓存降级")
            else:
                logger.warning("manifest 从缓存降级加载（evolution 不可用）")
            return cached
        return None

    def fetch_by_version(self, manifest_version: int) -> dict[str, Any] | None:
        """拉指定版本 manifest（A/B 回放用历史版本）。"""
        if not self._evolution_url:
            return None
        try:
            import httpx
            resp = httpx.get(
                f"{self._evolution_url}/api/manifests/{manifest_version}", timeout=5.0,
            )
            resp.raise_for_status()
            return self._enrich_with_content(resp.json())
        except Exception as exc:
            logger.warning("拉取 manifest v%s 失败: %s", manifest_version, exc)
            return None

    def _fetch_from_evolution(self) -> dict[str, Any] | None:
        try:
            import httpx
            resp = httpx.get(
                f"{self._evolution_url}/api/manifests/production", timeout=5.0,
            )
            if resp.status_code == 404:
                logger.warning("evolution 无 production manifest")
                return None
            resp.raise_for_status()
            return self._enrich_with_content(resp.json())
        except Exception as exc:
            logger.warning("从 evolution 拉 manifest 失败，降级: %s", exc)
            return None

    def _enrich_with_content(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """给 manifest entries 的每个 surface 填充 _content（逐个拉 surface 版本内容）。

        evolution 的 /api/manifests/production 返回 entries（只有版本指针），
        assemble 需要 content（A 类文本/B 类 JSON/C 类代码）。这里逐个拉。
        """
        entries = manifest.get("entries", manifest.get("entries_json_parsed", {}))
        # entries_json 可能是字符串（未解析）
        if isinstance(entries, str):
            entries = json.loads(entries)
        for surface in entries.get("surfaces", []):
            content = self._fetch_surface_content(
                surface["surface_type"], surface["surface_name"],
                surface["scope"], surface["version"],
            )
            if content is not None:
                surface["_content"] = content["content"]
                surface["_config"] = content.get("config", {})
        manifest["entries"] = entries
        return manifest

    def _fetch_surface_content(
        self, surface_type: str, surface_name: str, scope: str, version: int,
    ) -> dict[str, Any] | None:
        """拉单个 surface 版本的 content（调 evolution surface 详情端点）。"""
        try:
            import httpx
            # 用 surface 线 + 版本端点（surface_api 已提供）
            # 这里用通用查询：列出该线版本，取对应 version 的 content
            url = (f"{self._evolution_url}/api/surfaces/{surface_type}/"
                   f"{surface_name}/{scope}")
            resp = httpx.get(url, timeout=3.0)
            resp.raise_for_status()
            versions = resp.json()
            for v in versions:
                if v["version"] == version:
                    return {"content": v["content"], "config": json.loads(v.get("config") or "{}")}
            return None
        except Exception as exc:
            logger.warning("拉 surface %s/%s/%s v%s 失败: %s",
                           surface_type, surface_name, scope, version, exc)
            return None

    def _cache_path(self) -> Path:
        return self._cache_dir / "production_manifest.json"

    def _save_cache(self, manifest: dict[str, Any]) -> None:
        try:
            self._cache_path().write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
            )
        except OSError:
            pass

    def _load_cache(self) -> dict[str, Any] | None:
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


# ── 模块级单例 ──

_loader: ManifestLoader | None = None


def get_loader() -> ManifestLoader:
    """获取全局 ManifestLoader 单例（首次从 settings 初始化）。"""
    global _loader
    if _loader is None:
        from app.platform.core.settings import get_settings
        s = get_settings()
        _loader = ManifestLoader(
            evolution_url=getattr(s, "evolution_url", ""),
            cache_dir=getattr(s, "manifest_cache_dir", ".manifest_cache"),
        )
    return _loader


# ── assemble：manifest → AssembledManifest（数据层核心）─────────


def assemble(manifest: dict[str, Any]) -> AssembledManifest:
    """把 manifest JSON 解析成 AssembledManifest（装配意图）。

    执行端装配层（_assemble_via_manifest）读返回值后补 model/backend/checkpointer
    再调 create_deep_agent。本函数只做数据解析 + middleware 实例化 + C 类加载。

    Args:
        manifest: evolution 返回的 manifest（已 _enrich_with_content 填充 _content）

    Returns: AssembledManifest。
    """
    entries = manifest["entries"]
    surfaces = entries["surfaces"]

    # 按 (type, name, scope) 索引，方便按归属查
    index: dict[tuple[str, str, str], dict[str, Any]] = {
        (s["surface_type"], s["surface_name"], s["scope"]): s for s in surfaces
    }

    # ── meta 层装配 ──
    meta_prompt = _get_content(index, "prompt", "meta_system", "meta")
    meta_skills = _resolve_skills(index)
    meta_middleware = _instantiate_meta_middleware(index)

    # ── 各 subagent 装配 ──
    subagents: list[AssembledSubagent] = []
    # general-purpose（执行端用 GENERAL_PURPOSE_SUBAGENT，这里只标记）
    subagents.append(AssembledSubagent(kind="general_purpose", name="general-purpose"))

    # 4 个领域 subagent（按 scope 查其 description/prompt/middleware/permissions/deep_meta）
    for scope in ("interview", "storybuilding", "detail-outline", "writing"):
        sa = _assemble_one_subagent(index, scope)
        if sa is not None:
            subagents.append(sa)

    # ── 收集 state_schemas（从 C 类缓存取）──
    state_schemas: list[type] = []
    for key, cached in _c_surface_cache.items():
        if cached["state_schema"] is not None and cached["state_schema"] not in state_schemas:
            state_schemas.append(cached["state_schema"])

    return AssembledManifest(
        manifest_version=manifest["manifest_version"],
        meta_system_prompt=meta_prompt,
        meta_skills=meta_skills,
        meta_tools=[],
        meta_middleware_base=meta_middleware,
        meta_prompt_version=_get_meta_prompt_version(index),
        subagents=subagents,
        state_schemas=state_schemas,
        manifest_meta={
            "manifest_version": manifest["manifest_version"],
            "c_surfaces": entries.get("schema_lock", {}).get("c_surfaces", []),
        },
    )


def _get_content(
    index: dict[tuple[str, str, str], dict[str, Any]],
    surface_type: str, surface_name: str, scope: str,
) -> str:
    """从索引取 surface content（已 enrich 填充）。"""
    entry = index.get((surface_type, surface_name, scope))
    return entry.get("_content", "") if entry else ""


def _get_meta_prompt_version(
    index: dict[tuple[str, str, str], dict[str, Any]],
) -> int | None:
    """取 meta_system prompt 的 version（写 trace 用）。"""
    entry = index.get(("prompt", "meta_system", "meta"))
    return entry["version"] if entry else None


def _resolve_skills(
    index: dict[tuple[str, str, str], dict[str, Any]],
) -> list[str]:
    """解析 meta 层 skill surface → 绝对路径列表。

    meta 层 skills 只取 scope='meta' 的（auto-pipeline/interactive-gating）。
    各 subagent 的 skills 由 _resolve_skills_for_scope 单独取。
    skill surface 的 config.rel_dir 是相对 executor/app 的路径。
    """
    executor_app = Path(__file__).resolve().parent.parent.parent
    skills: list[str] = []
    for (surface_type, surface_name, scope), entry in index.items():
        if surface_type != "skill" or scope != "meta":
            continue
        rel_dir = entry.get("_config", {}).get("rel_dir", "")
        if rel_dir:
            skills.append(str(executor_app / rel_dir))
    return skills


def _instantiate_meta_middleware(
    index: dict[tuple[str, str, str], dict[str, Any]],
) -> list[Any]:
    """实例化 meta 层 middleware。

    meta 层 middleware 来源（调研结论）：
      - C 类（GoalMiddleware）：从 _c_surface_cache 取已加载实例
      - 其余（ErrorRecovery/MetaReadOnly）：执行端统一加（不进 manifest）

    所以 meta_middleware_base 这里只放 GoalMiddleware（C 类），
    ErrorRecovery/MetaReadOnly 由执行端 _assemble_via_manifest 补。
    """
    mw: list[Any] = []
    # C 类 GoalMiddleware（meta scope）
    goal_key = ("stateful_middleware", "GoalMiddleware", "meta")
    if goal_key in _c_surface_cache:
        mw.append(_c_surface_cache[goal_key]["instance"])
    return mw


def _assemble_one_subagent(
    index: dict[tuple[str, str, str], dict[str, Any]], scope: str,
) -> AssembledSubagent | None:
    """装配单个领域 subagent（按 scope 查其 surface 组合）。

    返回 AssembledSubagent，kind 由 scope 决定：
      - interview → custom（走 build_interview_deep_subagent）
      - storybuilding/detail-outline/writing → deep
    """
    # description
    desc = _get_content(index, "description", f"description/{scope}", scope)
    # system prompt（primary，不含 evaluation）
    prompt_name = _primary_prompt_name_for_scope(scope)
    system_prompt = _get_content(index, "prompt", prompt_name, scope)

    # skills（该 scope 的）
    skills = _resolve_skills_for_scope(index, scope)

    if scope == "interview":
        return AssembledSubagent(
            kind="custom", name="interview",
            description=desc, system_prompt=system_prompt, skills=skills,
        )

    # deep subagent：取 deep_meta + middleware_params
    deep_meta = _get_json_content(index, "middleware_params", f"deep_meta/{scope}", scope)
    subagent_mw = _instantiate_subagent_middleware(index, scope)
    return AssembledSubagent(
        kind="deep", name=scope,
        description=desc, system_prompt=system_prompt,
        subagent_middleware=subagent_mw, skills=skills,
        artifact_paths=deep_meta.get("artifact_paths", []) if deep_meta else [],
        max_revisions=deep_meta.get("max_revisions", 1) if deep_meta else 1,
        evaluator_kind=deep_meta.get("evaluator_kind", scope) if deep_meta else scope,
    )


def _primary_prompt_name_for_scope(scope: str) -> str:
    """scope → primary system prompt 的 surface_name。"""
    return {
        "interview": "interview_system",
        "storybuilding": "storybuilding_system",
        "detail-outline": "detail_outline_system",
        "writing": "writing_system",
    }.get(scope, f"{scope}_system")


def _resolve_skills_for_scope(
    index: dict[tuple[str, str, str], dict[str, Any]], scope: str,
) -> list[str]:
    """取某 scope 的 skill 绝对路径。"""
    from app.platform.core.settings import get_settings
    executor_app = Path(__file__).resolve().parent.parent.parent
    skills: list[str] = []
    for (surface_type, surface_name, sc), entry in index.items():
        if surface_type != "skill" or sc != scope:
            continue
        rel_dir = entry.get("_config", {}).get("rel_dir", "")
        if rel_dir:
            skills.append(str(executor_app / rel_dir))
    return skills


def _get_json_content(
    index: dict[tuple[str, str, str], dict[str, Any]],
    surface_type: str, surface_name: str, scope: str,
) -> dict[str, Any] | None:
    """取 B 类 surface 的 content（JSON 解析）。"""
    content = _get_content(index, surface_type, surface_name, scope)
    if not content:
        return None
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("B 类 surface %s/%s/%s JSON 解析失败", surface_type, surface_name, scope)
        return None


def _instantiate_subagent_middleware(
    index: dict[tuple[str, str, str], dict[str, Any]], scope: str,
) -> list[Any]:
    """实例化某 scope subagent 的 harness 自有 middleware（B 类参数化）。

    从 middleware_params surface 读参数 → 实例化对应 middleware 类。
    ${ctx.workspace_path} 占位符保留（执行端装配时替换为真实 workspace）。
    注意：这里实例化时 workspace 用占位符，执行端 _assemble_via_manifest
    会重新实例化（因为 workspace 是运行时值）——所以这里返回的是「规格」，
    实际实例化在执行端。

    为简化：本函数返回参数化的「待实例化规格」列表（dict 含 class + args），
    执行端读取后用真实 workspace 实例化。这避免 manifest_loader 持有 workspace。
    """
    specs: list[dict[str, Any]] = []
    for (surface_type, surface_name, sc), entry in index.items():
        if surface_type != "middleware_params" or sc != scope:
            continue
        # deep_meta 不是 middleware，跳过
        if surface_name.startswith("deep_meta/"):
            continue
        params = _get_json_content(index, surface_type, surface_name, scope)
        if params:
            specs.append(params)
    return specs  # type: ignore[return-value]  # 执行端识别 dict 规格并实例化
