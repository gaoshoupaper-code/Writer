#!/usr/bin/env python3
"""分层依赖 linter（PR-01 · 阶段 A 打地基）。

把《后端 Agent 架构重构 · 需求基准》第 144-152 行的 6 条分层铁律变成
机器可拦的检查。当前以 baseline 告警模式运行：存量违规登记在
``executor/layering_baseline.txt``，新增违规才会 fail（exit 1）。

铁律（来自需求基准文档 §分层依赖铁律）：
  R1 platform/ 不得 import domains/ 或 infrastructure/
  R2 domains/X/ 不得 import domains/Y/（X≠Y）
  R3 domains/ 只能 import platform/ + infrastructure/（+ 顶层 auth/admin/api/db/schemas 过渡期）
  R4 infrastructure/ 不得 import platform/domains（仅依赖自身 + 标准库）
  R5 core/ 不得 import writer/（基础设施层反向依赖业务层，PR-03 消除）
  R6 image domain 不得 import writer/（领域间反向依赖，PR-02 消除）

用法：
  python scripts/check_layering.py              # baseline 模式，只对新增违规 fail
  python scripts/check_layering.py --update     # 重新生成 baseline（消除存量后用）
  python scripts/check_layering.py --strict     # 严格模式，baseline 内的也 fail（重构完成后用）

退出码：
  0 = 通过（无新增违规 / 或 --update 成功）
  1 = 发现新增违规（baseline 模式）或存在任何违规（strict 模式）
  2 = 运行错误（解析失败、路径不存在等）
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_APP = REPO_ROOT / "executor" / "app"
BASELINE_FILE = REPO_ROOT / "executor" / "layering_baseline.txt"
# 共享契约包（执行端与进化端的单一真源，Phase 1 建立）
CONTRACTS_DIR = REPO_ROOT / "contracts"
# 进化端（Phase 2 拆层后：业务4层 + core 公共底座）
EVOLUTION_APP = REPO_ROOT / "evolution" / "app"
# evolution 业务层（core 不得依赖这些）
EVOLUTION_BUSINESS_LAYERS = {"ingestion", "diagnosis", "improvement", "view"}

# 三分层根（相对 executor/app/）
PLATFORM = "platform"
DOMAINS = "domains"
INFRA = "infrastructure"

# 允许 domains/ import 的"过渡期"顶层包（PR-16 schemas 下沉、PR-15 api 收敛后逐步收紧）
DOMAIN_ALLOWED_TOP = {
    "platform",
    "infrastructure",
    "db",            # Repository 层，PR-13 后归 infrastructure
    "schemas",       # PR-16 下沉到各 domain
    "auth",
    "admin",
    "core",          # PR-13 拆解前的过渡期
}

# 横切关注点（顶层独立，任意层可 import）
CROSSCUTTING = {"auth", "admin", "api"}


@dataclass(frozen=True)
class Violation:
    """一条分层违规。"""
    rule: str          # 规则编号，如 "R1"
    importer: str      # 违规文件相对 executor/app/ 的路径
    imported: str      # 被引用的模块，如 "app.writer.middleware"
    detail: str        # 人类可读说明


@dataclass
class ScanResult:
    """扫描结果。"""
    scanned: int
    violations: list[Violation]


# ── import 解析 ────────────────────────────────────────────────────────
def _module_path(file_path: Path) -> str:
    """文件 → 模块 dotted path（相对 executor/app/，去掉 app 前缀）。"""
    rel = file_path.relative_to(BACKEND_APP).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _file_to_dotted(file_path: Path) -> str:
    """文件 → app.X.Y 模块路径。"""
    return "app." + _module_path(file_path)


def _collect_app_files() -> list[Path]:
    """递归收集 executor/app/ 下所有 .py（排除 __pycache__）。"""
    return sorted(
        p for p in BACKEND_APP.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _collect_contracts_files() -> list[Path]:
    """递归收集 contracts/ 下所有 .py（排除 __pycache__）。"""
    if not CONTRACTS_DIR.exists():
        return []
    return sorted(
        p for p in CONTRACTS_DIR.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def scan_contracts() -> list[Violation]:
    """扫描 contracts/ 包，检查它是否违反「不依赖任何一端」的铁律。

    RC1: contracts/ 不得 import app.*（executor 或 evolution 的内部模块）。
         contracts 是共享契约层，必须零业务依赖。
         contracts 内部互引（contracts.trace → contracts.api 等）合法。

    与 executor 的 6 条铁律独立——contracts 不是 executor 的一部分。
    """
    violations: list[Violation] = []
    for file_path in _collect_contracts_files():
        imports = _parse_file_imports(file_path)
        # _parse_file_imports 只抽 app.* 模块；contracts 不应 import 任何 app.*
        for module in imports:
            if module.startswith("app."):
                rel = str(file_path.relative_to(REPO_ROOT)).replace("\\", "/")
                violations.append(Violation(
                    rule="RC1",
                    importer=rel,
                    imported=module,
                    detail="contracts/ 禁止依赖 app.*（executor/evolution 内部模块），契约层必须零业务依赖",
                ))
    return violations


def _collect_evolution_files() -> list[Path]:
    """递归收集 evolution/app/ 下所有 .py（排除 __pycache__）。"""
    if not EVOLUTION_APP.exists():
        return []
    return sorted(
        p for p in EVOLUTION_APP.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def scan_evolution() -> list[Violation]:
    """扫描 evolution/app/，检查进化端的分层铁律。

    RE1: evolution/app/core/ 不得 import 业务层（ingestion/diagnosis/improvement/view）。
         core 是公共底座（db/llm/settings/models），必须零业务依赖——否则会造成循环依赖
         （业务层依赖 core，core 又反过来依赖业务层）。

    evolution 的 4 个业务层之间目前允许跨层依赖（流水线阶段间有天然的数据流向，
    如 improvement 依赖 diagnosis 的结果）。后续如需更严格的层间隔离可追加规则。
    """
    violations: list[Violation] = []
    for file_path in _collect_evolution_files():
        # 判断是否在 core/ 下
        try:
            rel_to_app = file_path.relative_to(EVOLUTION_APP)
        except ValueError:
            continue
        parts = rel_to_app.parts
        if len(parts) < 2 or parts[0] != "core":
            continue  # 只检查 core/ 下的文件

        imports = _parse_file_imports(file_path)
        rel = str(file_path.relative_to(REPO_ROOT)).replace("\\", "/")
        for module in imports:
            # app.X.Y... → 取 X（第一层）
            segs = module.split(".")
            if len(segs) < 2 or segs[0] != "app":
                continue
            top_layer = segs[1] if len(segs) > 1 else ""
            if top_layer in EVOLUTION_BUSINESS_LAYERS:
                violations.append(Violation(
                    rule="RE1",
                    importer=rel,
                    imported=module,
                    detail=f"evolution core/ 禁止依赖业务层 {top_layer}/（公共底座不得反向依赖业务）",
                ))
    return violations


def _resolve_import_to_app_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    """从 import 节点抽出所有 `app.` 开头的模块引用。"""
    out: list[str] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("app."):
                out.append(alias.name)
    else:  # ast.ImportFrom
        if node.module and node.module.startswith("app."):
            out.append(node.module)
    return out


def _parse_file_imports(file_path: Path) -> list[str]:
    """解析单个文件的 AST，返回它 import 的所有 app.* 模块。"""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except SyntaxError as exc:
        print(f"[linter] 解析失败 {file_path}: {exc}", file=sys.stderr)
        sys.exit(2)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(_resolve_import_to_app_modules(node))
    return imports


# ── 层级判定 ────────────────────────────────────────────────────────────
def _top_segment(module: str) -> str | None:
    """app.a.b.c → 'a'。返回顶层包名。"""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "app":
        return None
    return parts[1] if len(parts) > 1 else None


def _domain_name(module: str) -> str | None:
    """app.domains.X.Y → 'X'。返回 domain 名（非 domains 则 None）。"""
    parts = module.split(".")
    if len(parts) >= 3 and parts[1] == DOMAINS:
        return parts[2]
    return None


def _first_layer(file_dotted: str) -> str | None:
    """app.platform.X → 'platform'。文件所在的第一层。"""
    return _top_segment(file_dotted)


# ── 6 条铁律检查 ────────────────────────────────────────────────────────
def _check(
    importer_rel: str,
    importer_dotted: str,
    imported_modules: list[str],
    violations: list[Violation],
) -> None:
    """对单个文件应用 6 条铁律。

    Args:
        importer_rel: 文件相对 executor/app/ 的真实路径（含 __init__.py）。
        importer_dotted: 文件的 app.X.Y 模块路径（用于判定所在 domain）。
    """
    layer = _first_layer(importer_dotted)
    importer_domain = _domain_name(importer_dotted)

    for module in imported_modules:
        target_top = _top_segment(module)
        if target_top is None:
            continue
        dst_domain = _domain_name(module)

        # 同 domain 内部子模块引用合法（如 domains.image.agent → domains.image.store）
        # 这条规则在所有 R3/R4 检查前统一豁免，避免误报。
        same_domain_internal = (
            target_top == DOMAINS
            and importer_domain is not None
            and dst_domain == importer_domain
        )

        # R1: platform 不得 import domains / infrastructure
        if layer == PLATFORM and target_top in (DOMAINS, INFRA):
            violations.append(Violation(
                rule="R1",
                importer=importer_rel,
                imported=module,
                detail=f"platform 层禁止依赖 {target_top}/ 层",
            ))

        # R2: domains/X 不得 import domains/Y（X≠Y）
        elif (
            layer == DOMAINS
            and target_top == DOMAINS
            and dst_domain is not None
            and importer_domain is not None
            and dst_domain != importer_domain
        ):
            violations.append(Violation(
                rule="R2",
                importer=importer_rel,
                imported=module,
                detail=f"domain '{importer_domain}' 禁止依赖 domain '{dst_domain}'",
            ))

        # R3: domains/ 只能 import platform/infrastructure + 过渡期白名单
        # writer 由专门的 R5/R6 管理（writer 本身是 PR-11 才降级的待迁移目录）。
        elif (
            layer == DOMAINS
            and not same_domain_internal
            and target_top != "writer"
            and target_top not in DOMAIN_ALLOWED_TOP
        ):
            violations.append(Violation(
                rule="R3",
                importer=importer_rel,
                imported=module,
                detail=f"domain 禁止依赖 {target_top}/（仅允许 platform/infrastructure/db/schemas/auth/admin/core）",
            ))

        # R4: infrastructure 不得 import platform/domains
        elif layer == INFRA and target_top in (PLATFORM, DOMAINS):
            violations.append(Violation(
                rule="R4",
                importer=importer_rel,
                imported=module,
                detail=f"infrastructure 层禁止依赖 {target_top}/ 层",
            ))

        # R5: core 不得 import writer（基础设施反向依赖业务）
        elif layer == "core" and target_top == "writer":
            violations.append(Violation(
                rule="R5",
                importer=importer_rel,
                imported=module,
                detail="core/ 禁止依赖 writer/（PR-03 切断循环）",
            ))

        # R6: image domain 不得 import writer（领域间反向依赖）
        elif importer_domain == "image" and target_top == "writer":
            violations.append(Violation(
                rule="R6",
                importer=importer_rel,
                imported=module,
                detail="domains/image 禁止依赖 writer/（PR-02 切断）",
            ))


# ── 扫描主流程 ──────────────────────────────────────────────────────────
def scan() -> ScanResult:
    """扫描全部 app/ 文件，返回违规列表。

    同一文件对同一模块的依赖（可能多行 import）合并为一条违规。
    """
    files = _collect_app_files()
    violations: list[Violation] = []
    seen: set[str] = set()
    for file_path in files:
        importer_dotted = _file_to_dotted(file_path)
        importer_rel = str(file_path.relative_to(BACKEND_APP)).replace("\\", "/")
        imports = _parse_file_imports(file_path)
        file_violations: list[Violation] = []
        _check(importer_rel, importer_dotted, imports, file_violations)
        for v in file_violations:
            key = _violation_key(v)
            if key in seen:
                continue
            seen.add(key)
            violations.append(v)
    violations.sort(key=lambda v: (v.rule, v.importer, v.imported))
    return ScanResult(scanned=len(files), violations=violations)


def _violation_key(v: Violation) -> str:
    """违规的稳定键，用于 baseline 比对。"""
    return f"{v.rule}|{v.importer}|{v.imported}"


def _format_violation(v: Violation) -> str:
    return f"[{v.rule}] {v.importer}: import {v.imported}  ({v.detail})"


def load_baseline() -> set[str]:
    """读取 baseline 文件，返回已登记的违规键集合。空文件 → 空集。"""
    if not BASELINE_FILE.exists():
        return set()
    keys: set[str] = set()
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keys.add(line)
    return keys


def update_baseline(violations: list[Violation]) -> None:
    """用当前违规列表重写 baseline 文件（--update 模式用）。"""
    lines = [
        "# 分层依赖 baseline（PR-01 生成）",
        "# 每行格式：RULE|importer|imported_module",
        "# 存量违规登记在此 = 允许；后续 PR 消除一条就删一行，最终此文件应为空。",
        "# 用 `python scripts/check_layering.py --update` 重新生成。",
        "",
    ]
    keys = sorted({_violation_key(v) for v in violations})
    lines.extend(keys)
    lines.append("")
    BASELINE_FILE.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分层依赖 linter")
    parser.add_argument(
        "--update", action="store_true",
        help="重新生成 baseline 文件（消除存量后用，会覆盖 executor/layering_baseline.txt）",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="严格模式：baseline 内的违规也算 fail（重构完成后用）",
    )
    args = parser.parse_args(argv)

    if not BACKEND_APP.exists():
        print(f"[linter] 找不到 {BACKEND_APP}", file=sys.stderr)
        return 2

    result = scan()

    if args.update:
        update_baseline(result.violations)
        print(f"[linter] baseline 已更新：{len(result.violations)} 条存量违规写入 {BASELINE_FILE.relative_to(REPO_ROOT)}")
        return 0

    baseline = load_baseline()
    current_keys = {_violation_key(v) for v in result.violations}

    # 新增违规 = 当前有但 baseline 没有
    new_violations = [v for v in result.violations if _violation_key(v) not in baseline]

    # 已消除 = baseline 有但当前没有（信息性提示）
    eliminated = baseline - current_keys

    print(f"[linter] 扫描 {result.scanned} 个文件，发现 {len(result.violations)} 条违规"
          f"（baseline {len(baseline)} 条 / 新增 {len(new_violations)} 条）。")

    if eliminated and not args.update:
        print(f"\n✅ {len(eliminated)} 条存量违规已消除（建议从 baseline 删除）：")
        for key in sorted(eliminated):
            print(f"    - {key}")

    if new_violations:
        print(f"\n❌ 发现 {len(new_violations)} 条【新增】违规（baseline 未登记）：", file=sys.stderr)
        for v in new_violations:
            print(f"  {_format_violation(v)}", file=sys.stderr)
        print(
            "\n如这些是重构过程中的预期违规，请确认后运行：\n"
            f"  python {Path(__file__).relative_to(REPO_ROOT)} --update\n"
            "（但通常新增违规 = 重新引入分层破坏，应修正而非登记）",
            file=sys.stderr,
        )
        return 1

    if args.strict and result.violations:
        print(f"\n❌ strict 模式：仍有 {len(result.violations)} 条存量违规未消除：", file=sys.stderr)
        for v in result.violations:
            print(f"  {_format_violation(v)}", file=sys.stderr)
        return 1

    # ── contracts 契约层独立性检查（Phase 1）──
    # contracts 不走 baseline：它是共享契约，任何 app.* 依赖都直接 fail。
    contracts_violations = scan_contracts()
    if contracts_violations:
        print(f"\n❌ contracts/ 契约层发现 {len(contracts_violations)} 条违规：", file=sys.stderr)
        for v in contracts_violations:
            print(f"  {_format_violation(v)}", file=sys.stderr)
        return 1

    # ── evolution 进化端分层检查（Phase 2）──
    # core/ 不得依赖业务层。evolution 不走 baseline（4层刚拆完，应零违规）。
    evolution_violations = scan_evolution()
    if evolution_violations:
        print(f"\n❌ evolution/ core 层发现 {len(evolution_violations)} 条违规：", file=sys.stderr)
        for v in evolution_violations:
            print(f"  {_format_violation(v)}", file=sys.stderr)
        return 1

    print("[linter] ✅ 通过：无新增分层违规。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
