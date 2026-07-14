"""ArtifactPrerequisiteMiddleware — 子代理前置产物校验中间件。

职责：
  在子代理开始执行前，校验其依赖的前置产物文件是否存在且非空。
  如果校验失败，直接抛出异常阻止子代理启动，避免代理在缺失上下文的情况下执行。

两种校验模式：
  1. 文件校验（markdown_directory=False）：
     检查单个文件是否存在且内容非空。
     例如：outline.md 必须存在且非空才能启动 writing 子代理。

  2. 目录校验（markdown_directory=True）：
     检查目录是否存在且包含至少一个非空的 .md 文件。
     例如：character/ 目录必须包含至少一个角色档案文件。

使用方式：
  在构建子代理时，将需要校验的前置产物以 ArtifactPrerequisite 列表传入。
  中间件会在 before_agent hook 中执行校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.runtime import Runtime


@dataclass(frozen=True)
class ArtifactPrerequisite:
    """前置产物描述。

    Attributes:
        label:              产物标签，用于错误消息中的人可读描述
        path:               产物文件或目录的绝对路径
        markdown_directory: 是否为目录校验模式（True 时检查目录中是否有非空 .md 文件）
    """
    label: str
    path: Path
    markdown_directory: bool = False


class ArtifactPrerequisiteMiddleware(AgentMiddleware):
    """子代理前置产物校验中间件。

    在子代理执行前（before_agent hook）检查所有前置产物是否就绪。
    任一产物缺失或为空都会抛出异常，阻止子代理启动。
    """

    def __init__(self, prerequisites: list[ArtifactPrerequisite]) -> None:
        """
        Args:
            prerequisites: 前置产物列表

        Raises:
            ValueError: 如果 prerequisites 为空列表
        """
        if not prerequisites:
            raise ValueError("ArtifactPrerequisiteMiddleware requires at least one prerequisite.")
        self.prerequisites = prerequisites

    def before_agent(self, state: object, runtime: Runtime[object]) -> dict[str, object] | None:
        """同步：子代理执行前的校验钩子。"""
        del state, runtime
        self._validate_prerequisites()
        return None

    async def abefore_agent(self, state: object, runtime: Runtime[object]) -> dict[str, object] | None:
        """异步：子代理执行前的校验钩子。"""
        del state, runtime
        self._validate_prerequisites()
        return None

    def _validate_prerequisites(self) -> None:
        """遍历所有前置产物，执行对应模式的校验。"""
        for prerequisite in self.prerequisites:
            if prerequisite.markdown_directory:
                _require_non_empty_markdown_directory(prerequisite.path, prerequisite.label)
            else:
                _require_non_empty_file(prerequisite.path, prerequisite.label)


def _require_non_empty_markdown_directory(path: Path, label: str) -> None:
    """校验目录存在且包含至少一个非空的 Markdown 文件。

    Args:
        path:  目录路径
        label: 产物标签（用于错误消息）

    Raises:
        FileNotFoundError: 目录不存在
        ValueError:        目录中没有非空的 Markdown 文件
    """
    if not path.is_dir():
        raise FileNotFoundError(f"Missing required {label} directory before subagent execution: {path}")
    # 检查是否有任意 .md 文件内容非空
    if any(child.is_file() and child.suffix == ".md" and child.read_text(encoding="utf-8").strip() for child in path.iterdir()):
        return
    raise ValueError(f"Missing required non-empty Markdown file in {label} directory before subagent execution: {path}")


def _require_non_empty_file(path: Path, label: str) -> None:
    """校验文件存在且内容非空。

    Args:
        path:  文件路径
        label: 产物标签（用于错误消息）

    Raises:
        FileNotFoundError: 文件不存在
        ValueError:        文件内容为空
    """
    if not path.is_file():
        raise FileNotFoundError(f"Missing required {label} file before subagent execution: {path}")
    if not path.read_text(encoding="utf-8").strip():
        raise ValueError(f"Required {label} file is empty before subagent execution: {path}")
