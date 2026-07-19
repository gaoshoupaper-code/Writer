"""git_sync —— executor 侧 harness 源码同步（Phase 8，Task 2.3）。

从 bare repo pull/clone harness 源码到本地目录，供 assemble 使用。

两种模式：
  - 生产路径：pull main 分支到固定 checkout 目录（reload 时更新）
  - 候选路径（A/B）：clone 指定 commit 到临时目录（每次独立，互不干扰）

设计依据：设计文档 D7a/D10b/D9a（assemble 的 source_root 参数）。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.platform.core.settings import get_settings

logger = logging.getLogger("writer.git_sync")

_GIT_AUTHOR = ("executor", "executor@local")


def _git(args: list[str], cwd: Path | None = None) -> str:
    """执行 git 命令，返回 stdout。失败 raise RuntimeError。"""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _project_root() -> Path:
    """项目根 Writer/（executor 的上一级目录）。"""
    # 本文件在 executor/app/platform/agent/git_sync.py，上四级是 Writer/
    return Path(__file__).resolve().parents[4]


def _resolve_path(p: str) -> Path:
    """相对路径基于项目根 Writer/ 解析（和 harness_package_path 一致）。"""
    path = Path(p)
    if not path.is_absolute():
        path = _project_root() / path
    return path.resolve()


def _clone_dir() -> Path:
    """executor 本地 clone 缓存根目录。"""
    path = _resolve_path(get_settings().harness_clone_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bare_repo() -> str:
    """bare repo 路径/URL（相对路径基于项目根解析）。"""
    return str(_resolve_path(get_settings().harness_bare_repo))


# ── 生产路径：pull main 到固定目录 ───────────────────────────────


def production_checkout() -> Path:
    """生产 checkout 目录路径（固定，pull main 分支）。"""
    return _clone_dir() / "production"


def pull_production() -> Path:
    """pull 或 clone main 分支到生产目录，返回该目录。

    首次：clone bare repo → production 目录。
    后续：pull origin main 更新。

    Returns:
        生产 checkout 目录（含完整 harness 包源码）
    """
    checkout = production_checkout()
    bare = _bare_repo()

    if checkout.exists() and (checkout / ".git").exists():
        # 已 clone，pull 更新
        _git(["fetch", "origin"], checkout)
        _git(["reset", "--hard", "origin/main"], checkout)
        logger.info("生产 harness 已 pull 更新: %s", checkout)
    else:
        # 首次 clone
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if checkout.exists():
            shutil.rmtree(checkout)
        _git(["clone", bare, str(checkout)])
        logger.info("生产 harness 首次 clone: %s", checkout)

    return checkout


def production_commit() -> str:
    """生产 checkout 目录当前的 commit hash（7 位短 hash）。"""
    checkout = production_checkout()
    if not (checkout / ".git").exists():
        return ""
    return _git(["rev-parse", "--short", "HEAD"], checkout)


# ── 候选路径：clone 指定 commit 到临时目录 ───────────────────────


def checkout_commit(commit: str) -> Path:
    """clone bare repo 并 checkout 指定 commit 到独立临时目录。

    用于 A/B 候选执行：每个候选 commit 一个独立目录，互不干扰（决策 D7a）。

    Args:
        commit: git commit hash（7 位或完整）

    Returns:
        临时 checkout 目录（含指定 commit 的源码）。调用方用完应 cleanup。

    Raises:
        RuntimeError: commit 在 bare repo 中不存在。错误信息附带 bare repo 最近
            几条 commit hash，帮助定位（evolution push 缺失 / commit hash 错写）。
    """
    bare = _bare_repo()
    tmp = Path(tempfile.mkdtemp(prefix=f"harness_{commit}_", dir=str(_clone_dir())))

    # clone 到临时目录
    _git(["clone", bare, str(tmp)])
    # checkout 指定 commit；失败时附 bare repo 当前可见的 commit 列表辅助定位
    # （线上踩过：evolution 端 commit 后未 push，executor clone 出来的仓库无此 commit）
    try:
        _git(["checkout", commit], tmp)
    except RuntimeError as exc:
        # 列出 bare repo 最近 10 个 commit，附在错误信息里方便诊断
        try:
            log = _git(["log", "--oneline", "-10"], tmp)
            available = "\n  ".join(log.splitlines()) if log else "(空仓库)"
        except Exception:  # noqa: BLE001
            available = "(无法读取 commit 列表)"
        # 清理失败的 checkout 临时目录，避免 _clone_dir 累积空目录
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            f"checkout commit {commit} 失败（bare repo 无此 commit）。"
            f"通常是 evolution 端 commit 未 push 到 bare repo 导致。\n"
            f"  原始错误: {exc.args[0]}\n"
            f"  bare repo 最近 commit:\n  {available}"
        ) from exc
    logger.info("候选 harness checkout: commit=%s → %s", commit, tmp)
    return tmp


def cleanup_checkout(path: Path) -> None:
    """清理 checkout 临时目录。"""
    clone_root = _clone_dir()
    # 安全检查：只清理 clone_dir 下的目录（防止误删）
    try:
        path.relative_to(clone_root)
    except ValueError:
        logger.warning("拒绝清理非 clone_dir 下的目录: %s", path)
        return
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        logger.debug("清理 checkout: %s", path)


__all__ = [
    "production_checkout",
    "pull_production",
    "production_commit",
    "checkout_commit",
    "cleanup_checkout",
]
