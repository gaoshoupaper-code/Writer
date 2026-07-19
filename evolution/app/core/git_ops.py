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
    _push_to_bare(wd, settings.harness_bare_repo_path)

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


def _push_to_bare(wd: Path, bare: Path) -> None:
    """把工作目录 main 推到 bare repo，处理非 fast-forward 漂移。

    正常情况 bare repo main 是工作目录的祖先，push 即可。但容器重建 /
    volume 漂移会让 bare 停在分叉的旧 commit 上，普通 push 报 non-fast-forward。
    此时用 --force-with-lease 兜底：工作目录是 harness 源码的唯一写入方，
    evolution 是单一真相源，强制对齐不会丢别处的提交。

    失败再 raise（commit_and_push 把异常透出，调用方按发版失败处理）。
    """
    try:
        _git(["push", "origin", "main"], wd)
    except RuntimeError as exc:
        if "non-fast-forward" not in exc.args[0] and "non fast-forward" not in exc.args[0]:
            raise
        logger.warning(
            "push 检测到 non-fast-forward（bare repo 漂移），用 --force-with-lease 对齐: %s",
            exc.args[0],
        )
        _git(["push", "--force-with-lease", "origin", "main"], wd)


def _sync_bare_to_head(wd: Path, bare: Path) -> None:
    """启动时对齐 bare repo main 与工作目录 HEAD。

    bare repo 是 executor clone 的源；只要它落后于 evolution 工作目录，
    executor 就拉不到 evolution 推导出的 commit hash，checkout 时报
    "pathspec did not match"。所以每次容器启动都要做一次对齐。

    策略（按从轻到重，统一交给 _push_to_bare 处理 fast-forward / 漂移）：
      1. bare 无 main（首次/被清空）→ 直接 push
      2. bare main == 工作目录 HEAD → 已同步，跳过
      3. 否则 → _push_to_bare（fast-forward 优先，分叉时 --force-with-lease）

    任何一步异常都记 warning 但不抛——启动不应被 git 同步卡死（自愈可延后）。
    """
    try:
        bare_heads = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "--verify", "main"],
            capture_output=True, text=True, timeout=10,
        )
        head = _git(["rev-parse", "HEAD"], wd)

        if bare_heads.returncode != 0:
            # bare repo 无 main（首次/被清空），首次 push
            _git(["push", "origin", "main"], wd)
            logger.info("首次 push main → bare repo")
            return

        bare_main = bare_heads.stdout.strip()
        if bare_main == head:
            return  # 已同步

        # bare 落后或分叉，统一交给 _push_to_bare（内部按 fast-forward / force-with-lease 处理）
        logger.info(
            "启动同步：bare repo 落后/分叉 (%s → %s)，触发 push",
            bare_main[:7], head[:7],
        )
        _push_to_bare(wd, bare)
    except Exception:
        logger.warning("启动时同步 bare repo 失败（不影响启动，后续 commit_and_push 会重试）",
                       exc_info=True)


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

    # 4. 确保 bare repo 的 main 与工作目录 HEAD 一致（executor pull 的前提）。
    #    旧逻辑只在 bare repo 完全没有 main 时才 push，无法自愈漂移——
    #    容器重建 / volume 重建 / 手动 commit 未 push 后，bare repo 会停在旧 commit，
    #    而 evolution 用 git log 推导版本→commit 时仍拿得到新 commit hash，
    #    发给 executor checkout 时就 pathspec did not match（线上已踩）。
    #    新逻辑：启动时强制对齐一次，保证 main 永远追上 HEAD（fast-forward 优先，
    #    漂移到工作目录更老时才需要 --force，但 init 阶段工作目录是真相源）。
    _sync_bare_to_head(wd, bare)


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
