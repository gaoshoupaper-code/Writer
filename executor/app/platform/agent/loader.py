"""Agent 包加载器（Phase 7 T3.1 + Phase 8 compose 热加载升级）。

执行端通过 importlib 加载 harness 包目录作为 Python package，
调用方取 mod.assemble(ctx) 装配完整 agent（单参数契约）。

加载机制（关键）：
  importlib.util.spec_from_file_location + submodule_search_locations。
  submodule_search_locations 是让包内相对 import（from .middleware import X）生效的
  关键——没有它，包被当作普通模块加载，相对 import 会失败。

Phase 8 变更（compose 配置化 + 热加载，决策 D10b/#16）：
  - 生产路径从"直读 evolution/harnesses/current/"改为"git pull bare repo → 加载 checkout 目录"
  - 新增 load_package(path) 通用函数：加载任意路径的包（候选 A/B 用）
  - 新增 reload_current()：清缓存 + git pull + 重新加载（不重启进程，决策 #16）
  - 候选路径：load_package(checkout_commit 返回的临时目录)

设计依据：设计文档 D8=X + D10b + #16 + D9a。
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

from app.platform.core.settings import get_settings

logger = logging.getLogger("writer.package_loader")

# 模块级缓存：生产包加载一次后复用（热加载时清缓存重建，决策 #16）
_loaded_package: ModuleType | None = None


def load_package(pkg_path: Path, mod_name: str = "harness_current") -> ModuleType:
    """加载指定路径的 Agent 包，返回包模块（含 assemble 函数）。

    通用加载函数：生产路径和候选 A/B 路径都用它，只是传不同的 pkg_path。
    submodule_search_locations 让包内相对 import 生效。

    Args:
        pkg_path: 包根目录（含 __init__.py）
        mod_name: 模块注册名（生产用 harness_current，候选用唯一名避免冲突）

    Returns:
        包模块对象，调用方取 mod.assemble(ctx) 装配 agent。

    Raises:
        FileNotFoundError: 包目录或 __init__.py 不存在。
        ImportError: 包 __init__.py 执行失败（含包内 import 错误）。
    """
    pkg_path = pkg_path.resolve()
    init_path = pkg_path / "__init__.py"
    if not init_path.exists():
        raise FileNotFoundError(f"Agent 包不存在: {init_path}")

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
        sys.modules.pop(mod_name, None)
        raise
    logger.info("Agent 包已加载: %s (mod=%s)", pkg_path, mod_name)
    return mod


def load_current_package() -> ModuleType:
    """加载生产 Agent 包（current），返回包模块。

    Phase 8：从 git pull 的生产 checkout 目录加载（非直读 evolution 工作目录）。
    幂等：首次加载后缓存，reload_current() 后重新加载。

    Returns: 包模块对象。
    """
    global _loaded_package
    if _loaded_package is not None:
        return _loaded_package

    # Phase 8：git pull 生产 checkout（决策 D10b）
    from app.platform.agent.git_sync import pull_production
    checkout = pull_production()
    _loaded_package = load_package(checkout, "harness_current")
    return _loaded_package


def reload_current() -> ModuleType:
    """热加载：清缓存 + git pull + 重新加载生产包（决策 #16，不重启进程）。

    evolution ship 后调 executor /reload 端点触发本函数。

    Returns: 重新加载后的包模块。
    """
    global _loaded_package
    # 清缓存：pop sys.modules 里包及其子模块（middleware.* 等）
    _purge_package_modules("harness_current")
    _loaded_package = None

    from app.platform.agent.git_sync import pull_production
    checkout = pull_production()
    _loaded_package = load_package(checkout, "harness_current")
    logger.info("生产包热加载完成: %s", checkout)
    return _loaded_package


def _purge_package_modules(prefix: str) -> None:
    """从 sys.modules 清除指定包前缀的所有模块（含子模块）。"""
    keys_to_remove = [k for k in sys.modules if k == prefix or k.startswith(prefix + ".")]
    for k in keys_to_remove:
        sys.modules.pop(k, None)


def reset_cache() -> None:
    """清除包缓存（测试用，或手动重载）。生产路径用 reload_current()。"""
    global _loaded_package
    if _loaded_package is not None:
        _purge_package_modules("harness_current")
        _loaded_package = None
