"""流程工具——进化 Agent 的评估消费 + 产出 + 校验（决策 S2/S9）。

阶段 D（2026-07-27）切断进化旁路：进化 Agent 只读评估卷宗，不读原始 trace /
完整证据卷宗。

工具：
  - read_eval_report()              读评估卷宗（findings + 冻结证据 + scores）
  - read_evidence_pack()            读评估卷宗的过程归因（frozen_evidence 片段）
  - write_design_doc(changes, rationale)  产 design_doc.md
  - validate_changes()              纯源码校验（py_compile + import）
  - write_change_log(applied, summary)    产 change_log.md

阶段 D 切断：read_trace / _read_memory_quality_summary 已移除（旁路）。
"""
from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.core.settings import settings
from app.evolve import docs
from app.evolve.ctx import get_tool_context

logger = logging.getLogger("evolution.evolve.agent.tools.flow")


# write_design_doc / write_change_log 的结构化入参（取代手写 JSON 字符串）。
# 模型 tool-call 时 API 层据 schema 组装，绕过手写嵌套 JSON 易错问题。
class DesignChange(BaseModel):
    """design_doc 的一条改动设计。"""

    target: str = Field(description="目标（要素路径，如 middleware/pacing.py 或 prompts/writing_system.md）")
    change_desc: str = Field(description="改什么（描述性）")
    reason: str = Field(description="依据评估证据（自然语言）")
    evidence_ref: list[str] = Field(description="引用评估 finding 的 id（必填，如 [\"f01\"]）")
    expected_up: str = Field(description="预期涨的方面")
    expected_down: str = Field(description="预期跌的方面（诚实声明）")


class AppliedRecord(BaseModel):
    """change_log 的一条落地记录。"""

    target: str = Field(description="改动目标")
    action: str = Field(description="落地动作：write_middleware|edit_source|write_prompt|...")
    result: str = Field(description="落地结果：ok|failed")
    detail: str = Field(description="细节")
    design_ref: int = Field(description="对应 design_doc 改动清单的序号（1-based）")


def make_flow_tools() -> list:
    """构建流程工具集（5 个）。"""

    @tool
    def read_eval_report() -> str:
        """读取当前 session 的评估报告（由评估 Agent 产出，已加载到上下文）。

        评估报告包含：
          - scores：内容层评分 + 流程硬指标
          - findings：诊断条目（每条含 id/dimension/severity/evidence_type/finding/evidence）
          - report_md：可读报告全文

        注意：评估只诊断问题（不含改进方案）。你据此设计改进方案。
        记下每条 finding 的 id（f01/f02…），write_design_doc 的 evidence_ref 要引用它。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.eval_snapshot:
            return "错误：评估报告未加载（eval_snapshot 为空）"
        snap = ctx.eval_snapshot
        scores = snap.get("scores", {})
        findings = snap.get("findings", [])
        report_md = snap.get("report_md", "")
        return (
            f"## 评估报告（trace={snap.get('trace_id', '?')}）\n\n"
            f"### 结构化分数\n```json\n{json.dumps(scores, ensure_ascii=False, indent=2)}\n```\n\n"
            f"### 诊断条目\n```json\n{json.dumps(findings, ensure_ascii=False, indent=2)}\n```\n\n"
            f"### 报告正文\n{report_md}"
        )

    @tool
    def read_evidence_pack() -> str:
        """读取评估卷宗引用的冻结证据片段（过程归因，阶段 D）。

        阶段 D 切断：进化 Agent 只读评估卷宗。本工具展示评估 finding 实际引用的
        冻结证据片段（封存时从证据卷宗冻结进评估卷宗，需求 §22），用于归因定位。
        不读原始 trace / 完整证据卷宗。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        if not ctx.eval_dossier:
            return "错误：评估卷宗未加载"
        dossier = ctx.eval_dossier
        frozen = dossier.get("frozen_evidence") or {}
        findings = dossier.get("findings") or []

        lines = ["# 评估卷宗 · 引用证据片段", ""]

        if not frozen:
            lines.append("本次评估未冻结证据片段（评估卷宗封存时无引用或证据卷宗无对应片段）。")
            lines.append("结合 read_eval_report 的 findings 理解问题。")
            return "\n".join(lines)

        # 按 finding 归组展示其引用的片段
        lines.append(f"## 冻结证据片段（共 {len(frozen)} 个）")
        for fid, snapshot in frozen.items():
            lines.append(f"### {fid}")
            lines.append(f"- type: {snapshot.get('type', '?')}")
            lines.append(f"- agent: {snapshot.get('agent_name', '?')}")
            lines.append(f"- sequence: {snapshot.get('sequence', '?')}")
            if snapshot.get("error"):
                lines.append(f"- error: {snapshot['error'][:200]}")
            if snapshot.get("tool_output"):
                lines.append(f"- tool_output: {snapshot['tool_output'][:400]}")
            if snapshot.get("output"):
                lines.append(f"- output: {snapshot['output'][:400]}")
            lines.append("")

        # 哪些 finding 引用了哪些片段
        lines.append("## finding → 证据引用")
        for f in findings:
            refs = f.get("evidence_ref") or f.get("evidence_id")
            if isinstance(refs, str):
                refs = [refs]
            if isinstance(refs, list) and refs:
                lines.append(f"- {f.get('id', '?')}: {', '.join(str(r) for r in refs)}")
        lines.append("")
        lines.append("结合 read_eval_report 的 findings 诊断 + 这里的证据片段定位改进点。")

        return "\n".join(lines)

    @tool
    def write_design_doc(changes: list[DesignChange], rationale: str) -> str:
        """产出改动设计文档 design_doc.md。

        基于评估诊断设计具体改动方案。每个改动必须是可落地的具体指令。
        这应在实际改代码之前调用——先想清楚改什么、为什么改，再动手。

        Args:
            changes: 改动列表。每条含：
              - target / change_desc / reason / evidence_ref / expected_up / expected_down
              **evidence_ref 是硬性必填**：每个改动必须引用至少一个评估 finding 的 id，
              证明"为什么改"。id 格式 f01/f02…，从 read_eval_report 的 findings 里取。
            rationale: 自然语言总述（基于评估报告的整体判断，为什么这么改）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_design_doc", "running")
        try:
            if not changes:
                return "changes 不能为空（至少一个改动）"

            # 提取评估报告里的合法 finding id 集合
            valid_finding_ids: set[str] = set()
            snap = ctx.eval_snapshot or {}
            findings = snap.get("findings") or []
            for f in findings:
                if isinstance(f, dict) and f.get("id"):
                    valid_finding_ids.add(str(f["id"]))

            # 死局短路：评估报告无可用 finding id
            if not valid_finding_ids:
                ctx.emit_step("write_design_doc", "failed", reason="no_findings")
                return (
                    "评估报告没有可引用的结构化 finding（findings 为空或无 id）。"
                    "evidence_ref 校验无法满足，无法产出 design_doc。"
                    "这是评估阶段的问题——请结束当前进化，提示重新评估该 trace 后再启动进化。"
                )

            # Pydantic 已保证字段齐全，这里校验业务约束：evidence_ref 必须引用真实 finding id
            for i, c in enumerate(changes):
                if not c.evidence_ref:
                    return (
                        f"changes[{i}] 缺少 evidence_ref：每个改动必须引用至少一个评估 "
                        f"finding 的 id。请先 read_eval_report 拿到 finding id。"
                    )
                bad = [r for r in c.evidence_ref if str(r) not in valid_finding_ids]
                if bad:
                    return (
                        f"changes[{i}] 的 evidence_ref {bad} 不存在于评估报告的 finding 列表中。"
                        f"合法 id：{sorted(valid_finding_ids) or '（无）'}。"
                    )

            # 转 list[dict] 传给 docs 层（落盘契约不变）
            changes_dicts: list[dict[str, Any]] = [c.model_dump() for c in changes]
            path = docs.write_design_doc(ctx.session_id, changes=changes_dicts, rationale=rationale)
            ctx.design_doc_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, design_doc_path=path)
            ctx.emit_step("write_design_doc", "done", path=path, changes=len(changes))
            return f"设计文档已产出：{path}（{len(changes)} 个改动）"
        except Exception as e:
            ctx.emit_step("write_design_doc", "failed", error=str(e))
            return f"产出设计文档失败：{e}"

    @tool
    def validate_changes() -> str:
        """校验 harness 包源码改动的合法性。

        在所有改动落地后（write_*/edit_source）调用。校验项：
          1. py_compile：harness 包内所有 .py 文件无语法错误。
          2. import 检查：尝试 import 改动过的模块，捕获运行时错误
             （如引用不存在的模块、类定义错误）。

        如果校验失败，按错误信息修复后重新校验。
        **建议最多调用 2 次**——若 2 次仍失败，如实写 change_log 收尾。
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("validate_changes", "running")
        errors: list[str] = []
        env_diffs: list[str] = []
        pkg_root = settings.harness_work_dir_path

        # 1. py_compile 全包源码
        import py_compile
        for py in pkg_root.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            try:
                py_compile.compile(str(py), doraise=True)
            except py_compile.PyCompileError as e:
                rel = py.relative_to(pkg_root)
                errors.append(f"语法错误 {rel}: {e}")

        # 2. import 检查：环境差异（app.platform.* 等 evolution 不具备的框架包）单独归类，
        #    不算校验失败——它们在 executor 运行时能正常 import。
        _import_check_all(pkg_root, errors, env_diffs)

        # 3. FR-002 / EDGE-002 落地原子性校验：__init__.py 里构造的中间件，其实际
        #    __init__ 签名必须接受构造调用传的 kwargs。拦截签名漂移。
        _middleware_signature_check(pkg_root, errors)

        # passed 只看真错误：env_diffs 是环境差异，不阻塞 FlowGuard
        passed = len(errors) == 0
        ctx.emit_step(
            "validate_changes", "done" if passed else "failed",
            passed=passed, errors=len(errors), env_diffs=len(env_diffs),
        )
        if passed and not env_diffs:
            return "校验通过：harness 包所有源码无语法错误 + import 正常。"
        if passed:
            # 无真错误，但有环境差异——如实标注哪些框架包未校验，让 Agent 知道
            # 这些不是它的错，不用反复改。
            return (
                f"校验通过：harness 包语法正常、无自身 import 错误。\n"
                f"（{len(env_diffs)} 项框架包引用因运行环境差异未校验，属正常："
                f"app.platform / app.schemas 等 executor 私有包 evolution 端不具备。"
                f"这些在 executor 运行时可正常 import，无需修改。）"
            )
        return "校验失败，发现以下问题：\n" + "\n".join(f"- {e}" for e in errors)

    @tool
    def write_change_log(applied: list[AppliedRecord], summary: str) -> str:
        """产出执行改动记录 change_log.md。这应是你最后一步。

        注意：write_design_doc 必须在 write_change_log 之前完成（FlowGuard 会强制检查）。

        Args:
            applied: 已落地改动列表。每条含 target/action/result/detail/design_ref
              （design_ref 对应 design_doc 改动清单的序号，1-based）。
            summary: 自然语言总述（落地了什么、是否通过校验）
        """
        ctx = get_tool_context()
        if ctx is None:
            return "错误：session 未初始化"
        ctx.emit_step("write_change_log", "running")
        try:
            # 转 list[dict] 传给 docs 层（落盘契约不变）
            applied_dicts: list[dict[str, Any]] = [a.model_dump() for a in applied]
            validation = {"passed": True, "errors": []}
            path = docs.write_change_log(
                ctx.session_id,
                applied=applied_dicts,
                validation=validation,
                summary=summary,
            )
            ctx.change_log_path = path
            from app.evolve import db as ev_db
            ev_db.update_session(ctx.session_id, change_log_path=path)
            ctx.emit_step("write_change_log", "done", path=path, applied=len(applied))
            return f"改动记录已产出：{path}（{len(applied)} 个改动落地）"
        except Exception as e:
            ctx.emit_step("write_change_log", "failed", error=str(e))
            return f"产出记录失败：{e}"

    return [read_eval_report, read_evidence_pack, write_design_doc, validate_changes, write_change_log]


# ── import 检查辅助 ─────────────────────────────────────────────


# harness 包运行在 executor 进程，会 import executor 私有的框架包
# （app.platform.* / app.schemas.* / app.domains.* / app.routers.*）。
# evolution 进程不具备这些包（容器里不打包 executor 源码），校验时必然
# ModuleNotFoundError——这是「运行环境差异」，不是 harness 代码错误。
# 校验聚焦 harness 自身（语法 + 相对 import + 符号引用），框架包缺失
# 单独归类为 env_diffs，不污染真错误清单、不阻塞 FlowGuard。
_FRAMEWORK_PKGS_NOT_IN_EVOLUTION = frozenset({
    "app.platform", "app.schemas", "app.domains", "app.routers",
    "app.harnesses", "app.admin", "app.auth",
})


def _is_env_diff(exc: BaseException) -> bool:
    """判断一个 import 异常是否源于 evolution 不具备的框架包（环境差异）。

    ModuleNotFoundError.name 是缺失的模块全名，与白名单精确前缀比对。其余异常
    （AttributeError 引用不存在符号、ImportError 循环依赖、harness 自身相对
    import 拼错）一律归真错误——这些才是 validate_changes 该抓的。
    """
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        for fw in _FRAMEWORK_PKGS_NOT_IN_EVOLUTION:
            if exc.name == fw or exc.name.startswith(fw + "."):
                return True
    return False


def _import_check_all(pkg_root: Path, errors: list[str], env_diffs: list[str]) -> None:
    """尝试 import harness 包内所有 .py 模块，分类捕获异常。

    用与 executor loader 一致的机制先把包加载为 harness_current
    （spec_from_file_location + submodule_search_locations），否则裸
    import_module("harness_current.xxx") 因顶层包未注册必然全部失败。

    异常分两类：
      - errors：harness 自身的问题（语法已过但 import 时引用不存在的同包模块、
        符号名拼错、相对 import 循环）。这些必须修。
      - env_diffs：harness 引用的框架级外部包（app.platform.* 等）在 evolution
        进程不存在——属环境差异，不是 harness 代码错误。
    """
    import importlib.util

    # 先把包根注册为 harness_current（若未加载），让包内相对 import 生效。
    # 已加载则先清旧缓存，确保校验的是磁盘当前内容（含本次改动）。
    _purge = [k for k in list(sys.modules) if k == "harness_current" or k.startswith("harness_current.")]
    for k in _purge:
        del sys.modules[k]

    init_path = pkg_root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "harness_current", init_path, submodule_search_locations=[str(pkg_root)],
    )
    if spec is None or spec.loader is None:
        errors.append(f"无法创建包加载 spec: {init_path}")
        return
    pkg_mod = importlib.util.module_from_spec(spec)
    sys.modules["harness_current"] = pkg_mod
    try:
        spec.loader.exec_module(pkg_mod)
    except Exception as e:
        # 包 __init__ 本身执行失败——按环境差异/真错误分类
        (env_diffs if _is_env_diff(e) else errors).append(f"import 跳过 __init__.py: {e}")
        return

    # 收集所有 .py 相对路径 → 模块名（跳过 __init__.py，上面已 exec）
    py_files = [
        p for p in pkg_root.rglob("*.py")
        if "__pycache__" not in p.parts and p.name != "__init__.py"
    ]
    for py in py_files:
        rel = py.relative_to(pkg_root)
        # 构造模块名：harness_current.subagents.storybuilding 等
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]  # 去 .py
        mod_name = "harness_current." + ".".join(parts)
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            (env_diffs if _is_env_diff(e) else errors).append(
                f"import {'环境差异' if _is_env_diff(e) else '错误'} {rel}: {e}"
            )


def _middleware_signature_check(pkg_root: Path, errors: list[str]) -> None:
    """FR-002 / EDGE-002：校验 __init__.py 构造中间件的调用与类定义签名一致。

    遍历 __init__.py 里所有 `XxxMiddleware(...)` 构造调用，对每个调用校验
    类的 __init__ 是否接受调用传的 keyword 参数。拦截"改了 __init__.py 调用处
    但漏改 middleware 定义"的签名漂移（probe_candidate 装配时才 TypeError）。
    """
    import ast
    import inspect

    init_path = pkg_root / "__init__.py"
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return  # py_compile 已报过语法错误

    pkg = sys.modules.get("harness_current")
    if pkg is None:
        return  # _import_check_all 失败时包未注册，那条错误已记录

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name):
            continue
        class_name = func.id
        if not class_name.endswith("Middleware"):
            continue
        kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
        if not kw_names:
            continue

        klass = getattr(pkg, class_name, None)
        if klass is None:
            continue
        try:
            own_init = "__init__" in klass.__dict__
            sig = inspect.signature(klass.__init__)
            params = sig.parameters
            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if has_var_keyword and own_init:
                continue  # 类自己定义 __init__(**kwargs)，显式接受任意 keyword
            if not own_init:
                errors.append(
                    f"签名漂移 {class_name}: 类未定义自己的 __init__，"
                    f"但 __init__.py 构造时传了 keyword 参数 {kw_names}。"
                    f"基类 __init__ 可能不接受（→ TypeError: takes no arguments）。"
                    f"请确认基类签名或为 {class_name} 补 __init__。"
                )
                continue
            accepted = set(params.keys()) - {"self"}
            unknown = [k for k in kw_names if k not in accepted]
            if unknown:
                errors.append(
                    f"签名漂移 {class_name}: __init__ 不接受参数 {unknown}，"
                    f"但 __init__.py 构造时传了。可接受: {sorted(accepted) or '(无参数)'}。"
                    f"这是落地原子性违约——probe 装配会 TypeError。"
                )
        except (ValueError, TypeError) as e:
            logger.debug("中间件 %s 签名内省失败: %s", class_name, e)


__all__ = ["make_flow_tools"]
