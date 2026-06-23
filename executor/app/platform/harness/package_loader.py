"""Agent 包加载器（Phase 7 T3.1，D8=X 生产路径）。

执行端同进程 import evolution/harnesses/current/ 作为 Python package，
调 package.assemble(ctx) 装配完整 agent。

加载机制（关键）：
  importlib.util.spec_from_file_location + submodule_search_locations。
  submodule_search_locations 是让包内相对 import（from .middleware import X）生效的
  关键——没有它，包被当作普通模块加载，相对 import 会失败。

  与旧 manifest_loader（从 DB 拉 surface content）的区别：
  - manifest_loader：fetch_production → _enrich_with_content → assemble（数据来自 DB）
  - package_loader：importlib 加载目录 → package.assemble(ctx)（数据来自包目录）

  本模块只管"把包加载进 Python 解释器"，装配逻辑在包内 __init__.py:assemble。

A/B/回放路径（D7=② 子进程隔离）不经过本模块——那些在 worker 子进程里按
解压后的临时目录加载。本模块只服务生产热路径（current 包）。

设计依据：设计文档 D8=X（生产同进程）+ D1=B（包自带 assemble）。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from app.platform.core.settings import get_settings

logger = logging.getLogger("writer.package_loader")

# 模块级缓存：加载一次后复用（current 包进程内不变，换版本需重启——D11 设计）
_loaded_package: ModuleType | None = None


def load_current_package() -> ModuleType:
    """加载 Agent 包（current），返回包模块（含 assemble 函数）。

    幂等：首次加载后缓存，重复调用返回同一模块实例。
    current 包进程内不变（D11：换版本需重启进程），故缓存安全。

    Returns: 包模块对象，调用方取 mod.assemble(ctx) 装配 agent。

    Raises:
        FileNotFoundError: 包目录或 __init__.py 不存在。
        ImportError: 包 __init__.py 执行失败（含包内 import 错误）。
    """
    global _loaded_package
    if _loaded_package is not None:
        return _loaded_package

    s = get_settings()
    pkg_path = Path(s.harness_package_path)
    if not pkg_path.is_absolute():
        # 相对项目根 Writer/（executor/ 的上一级）
        pkg_path = Path(__file__).resolve().parents[4] / pkg_path
    pkg_path = pkg_path.resolve()

    init_path = pkg_path / "__init__.py"
    if not init_path.exists():
        raise FileNotFoundError(
            f"Agent 包不存在: {init_path}。请确认 evolution/harnesses/current/ 已建立。"
        )

    mod_name = "harness_current"
    # submodule_search_locations：让包内相对 import 生效的关键。
    # spec_from_file_location 不带此参数时，包被当普通模块，from .middleware import 会失败。
    spec = importlib.util.spec_from_file_location(
        mod_name,
        init_path,
        submodule_search_locations=[str(pkg_path)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建包加载 spec: {init_path}")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # 加载失败要清理 sys.modules，避免半加载状态残留
        sys.modules.pop(mod_name, None)
        raise
    _loaded_package = mod
    logger.info("Agent 包已加载: %s (assemble 可用)", pkg_path)
    return mod


def reset_cache() -> None:
    """清除包缓存（测试用，或手动重载）。生产路径不调（D11：换版本重启进程）。"""
    global _loaded_package
    if _loaded_package is not None:
        sys.modules.pop("harness_current", None)
        _loaded_package = None
