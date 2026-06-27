"""git_ops —— evolution 侧 harness git 操作（Phase 8，Task 2.2）。

封装 evolution 对 harness 工作目录的 git 操作：commit 变更 + push 到 bare repo。

工作目录（harnesses/current/）是 evolution 编辑源码的地方（决策 D11a）。
每次 ship 或产候选时，evolution commit 变更并 push 到 bare repo（决策 D7a/D10b）。
executor 从 bare repo pull/clone（在 git_sync.py 实现）。

设计依据：设计文档 D7a/D10b/D11a。
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from app.core.settings import settings

logger = logging.getLogger("evolution.git_ops")

# git 操作的统一 author（避免依赖全局 git config）
_GIT_AUTHOR = ("evolution", "evolution@local")


def _git(args: list[str], cwd: Path) -> str:
    """执行 git 命令，返回 stdout。失败 raise RuntimeError。"""
    cmd = ["git"] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _git_with_author(args: list[str], cwd: Path) -> str:
    """执行 git 命令（带统一 author，避免依赖全局 git config）。"""
    cmd = [
        "git",
        "-c", f"user.name={_GIT_AUTHOR[0]}",
        "-c", f"user.email={_GIT_AUTHOR[1]}",
    ] + args
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败 (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def work_dir() -> Path:
    """harness 工作目录路径。"""
    return settings.harness_work_dir_path


def has_changes() -> bool:
    """工作目录是否有未提交变更。"""
    out = _git(["status", "--porcelain"], work_dir())
    return bool(out.strip())


def commit_and_push(message: str) -> str:
    """commit 工作目录变更 + push 到 bare repo，返回 commit hash。

    如果无变更，跳过 commit，返回当前 HEAD。

    Args:
        message: commit message

    Returns:
        commit hash（7 位短 hash）
    """
    wd = work_dir()

    if not has_changes():
        logger.debug("工作目录无变更，跳过 commit")
        return current_commit()

    _git_with_author(["add", "-A"], wd)
    _git_with_author(["commit", "-m", message], wd)
    _git(["push", "origin", "main"], wd)

    commit_hash = current_commit()
    logger.info("harness 变更已 commit + push: %s (%s)", message, commit_hash)
    return commit_hash


def current_commit() -> str:
    """当前工作目录 HEAD 的 commit hash（7 位短 hash）。"""
    return _git(["rev-parse", "--short", "HEAD"], work_dir())


def commit_file(file_path: str, content: str, message: str) -> str:
    """写入文件内容 + commit + push（用于 Evolver 产新源码）。

    Args:
        file_path: 相对工作目录的路径（如 "middleware/pacing.py"）
        content:   文件内容
        message:   commit message

    Returns:
        commit hash
    """
    wd = work_dir()
    abs_path = wd / file_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    return commit_and_push(message)


def init_work_repo() -> None:
    """初始化工作目录为 git 仓库（首次部署用，幂等）。

    如果工作目录已 git init 且 remote 已配置，跳过。
    """
    wd = work_dir()
    bare = settings.harness_bare_repo_path

    # 确保 bare repo 存在
    if not bare.exists():
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        logger.info("bare repo 创建: %s", bare)

    # 初始化工作目录 git
    git_dir = wd / ".git"
    if not git_dir.exists():
        _git(["init"], wd)
        _git(["branch", "-M", "main"], wd)
        logger.info("工作目录 git init: %s", wd)

    # 配置 remote
    remotes = _git(["remote"], wd)
    if "origin" not in remotes.split():
        _git(["remote", "add", "origin", str(bare)], wd)
    else:
        _git(["remote", "set-url", "origin", str(bare)], wd)


__all__ = [
    "work_dir",
    "has_changes",
    "commit_and_push",
    "current_commit",
    "commit_file",
    "init_work_repo",
]
