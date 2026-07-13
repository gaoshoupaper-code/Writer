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


def log_oneline(limit: int = 200) -> list[str]:
    """工作目录 git log（倒序：最新在前），每行一条 "短hash message"。

    用于 registry 的 version↔commit 映射（version N = 第 N 个 commit）。
    """
    out = _git(["log", "--oneline", f"-{limit}"], work_dir())
    return out.splitlines() if out.strip() else []


def show_file(commit: str, file_path: str) -> str:
    """读取指定 commit 版本下的文件内容（git show <commit>:<path>）。

    用于评估 Agent 读历史 snapshot 版本的源码要素（决策 V1）。

    Args:
        commit:    commit hash（snapshot 的 source_commit）
        file_path: 相对工作目录的路径（如 "middleware/pacing.py"）

    Returns:
        文件全文。文件不存在该 commit 时 raise RuntimeError。
    """
    return _git(["show", f"{commit}:{file_path}"], work_dir())


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
    """初始化 harness 独立仓库 + bare repo（首次部署用，幂等）。

    新架构（去 DB 重构）：repo/ 是独立的 harness git 仓库，bare repo 是
    evolution push / executor pull 的中转。本函数确保两者就绪：
      1. bare repo 存在（git init --bare）
      2. 工作目录 repo/ 是 git 仓库（首次/空 volume 时从镜像内容初始化）
      3. remote origin 指向 bare repo
      4. main 分支代码已 push 到 bare repo（executor 才能 pull production）
    """
    wd = work_dir()
    bare = settings.harness_bare_repo_path

    # 1. 确保 bare repo 存在 + HEAD 指向 main
    bare.mkdir(parents=True, exist_ok=True)
    if not (bare / "HEAD").exists():
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            check=True, capture_output=True,
        )
        logger.info("bare repo 创建: %s", bare)
    # bare repo 的 HEAD 必须指向 main，否则 executor clone 时 checkout 失败
    # （git init --bare 默认 HEAD=master，push main 后 HEAD 仍指向不存在的 master）
    head_file = bare / "HEAD"
    if head_file.read_text(encoding="utf-8").strip() != "ref: refs/heads/main":
        head_file.write_text("ref: refs/heads/main\n", encoding="utf-8")
        logger.info("bare repo HEAD → refs/heads/main")

    # 2. 确保工作目录是 git 仓库
    #    新部署/空 volume 时 repo/ 可能只有源码文件但没有 .git（docker volume
    #    首次复制只拷文件不拷 .git），需要重新 init 并做首次 commit。
    git_dir = wd / ".git"
    if not git_dir.exists():
        _git(["init"], wd)
        _git(["branch", "-M", "main"], wd)
        # 首次提交：把镜像自带的源码 + registry.json 固化为 v1
        _git_with_author(["add", "-A"], wd)
        result = subprocess.run(
            ["git", "-c", f"user.name={_GIT_AUTHOR[0]}",
             "-c", f"user.email={_GIT_AUTHOR[1]}",
             "commit", "-m", "harness 仓库初始化（首次部署）"],
            cwd=wd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("工作目录首次 commit: %s", wd)
        # 空目录无文件可提交不是错误（异常情况会日志记录）

    # 3. 配置 remote（幂等：有则 set-url，无则 add）
    remotes = _git(["remote"], wd)
    if "origin" not in remotes.split():
        _git(["remote", "add", "origin", str(bare)], wd)
    else:
        _git(["remote", "set-url", "origin", str(bare)], wd)

    # 4. 确保 main 已 push 到 bare repo（executor pull 的前提）
    #    用 --force 只在首次初始化场景：正常演进用 commit_and_push（fast-forward）。
    #    这里幂等保护：bare repo 无 main 时才 push，避免覆盖已有历史。
    try:
        bare_heads = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "--verify", "main"],
            capture_output=True, text=True, timeout=10,
        )
        if bare_heads.returncode != 0:
            # bare repo 无 main，首次 push
            _git(["push", "origin", "main"], wd)
            logger.info("首次 push main → bare repo")
    except Exception:
        logger.warning("检查 bare repo main 失败，尝试 push", exc_info=True)
        _git(["push", "origin", "main"], wd)


__all__ = [
    "work_dir",
    "has_changes",
    "commit_and_push",
    "current_commit",
    "log_oneline",
    "show_file",
    "commit_file",
    "init_work_repo",
]
