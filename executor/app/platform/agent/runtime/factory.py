"""platform.agent.runtime.factory —— DeepAgents agent 工厂隔离层（PR-08）。

re-export create_deep_agent（DeepAgents 的核心 agent 构建函数），让领域层
从这里 import 而非直接碰 deepagents。未来换框架时只改本文件。

注意：写作专属的装配逻辑（build_deep_subagent，含 evolution subagent +
RevisionLimitMiddleware + ArtifactValidationMiddleware）仍在
writer/expert_agent/factory.py——那是领域逻辑，不属于 runtime 隔离层。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

from app.platform.agent.middleware.artifact_capture import PlatformArtifactCaptureMiddleware
from deepagents import create_deep_agent as _create_deep_agent
from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT


@dataclass(frozen=True)
class _ArtifactCaptureBinding:
    recorder: Any
    trace_id: str
    workspace_root: Path
    strict: bool


_ARTIFACT_CAPTURE_BINDING: ContextVar[_ArtifactCaptureBinding | None] = ContextVar(
    "platform_artifact_capture_binding", default=None
)


@contextmanager
def artifact_capture_scope(
    *, recorder: Any, trace_id: str, workspace_root: Path, strict: bool
) -> Iterator[None]:
    """让装配期间的所有 DeepAgent（含嵌套/review）继承平台取证。"""
    token = _ARTIFACT_CAPTURE_BINDING.set(
        _ArtifactCaptureBinding(
            recorder=recorder,
            trace_id=trace_id,
            workspace_root=workspace_root,
            strict=strict,
        )
    )
    try:
        yield
    finally:
        _ARTIFACT_CAPTURE_BINDING.reset(token)


@wraps(_create_deep_agent)
def create_deep_agent(*args: Any, **kwargs: Any) -> Any:
    """在 DeepAgents 编译前注入 executor 不可回退的最低取证能力。"""
    binding = _ARTIFACT_CAPTURE_BINDING.get()
    if binding is None:
        return _create_deep_agent(*args, **kwargs)

    middleware = list(kwargs.get("middleware") or [])
    agent_name = _infer_agent_name(middleware, kwargs.get("name"))
    kwargs["middleware"] = _with_capture(middleware, binding, agent_name)

    subagents = kwargs.get("subagents")
    if subagents is not None:
        injected: list[Any] = []
        for subagent in subagents:
            if not isinstance(subagent, dict) or "runnable" in subagent:
                injected.append(subagent)
                continue
            spec = dict(subagent)
            spec_middleware = list(spec.get("middleware") or [])
            spec_name = str(spec.get("name") or "subagent")
            spec["middleware"] = _with_capture(spec_middleware, binding, spec_name)
            injected.append(spec)
        kwargs["subagents"] = injected

    return _create_deep_agent(*args, **kwargs)


def _with_capture(
    middleware: list[Any], binding: _ArtifactCaptureBinding, agent_name: str
) -> list[Any]:
    if any(isinstance(item, PlatformArtifactCaptureMiddleware) for item in middleware):
        return middleware
    capture = PlatformArtifactCaptureMiddleware(
        recorder=binding.recorder,
        trace_id=binding.trace_id,
        workspace_root=binding.workspace_root,
        agent_name=agent_name,
        strict=binding.strict,
    )
    result = [capture, *middleware]
    record_stack = getattr(binding.recorder, "record_middleware_assembly", None)
    if callable(record_stack):
        record_stack(binding.trace_id, agent_name, result)
    return result


def _infer_agent_name(middleware: list[Any], explicit_name: object) -> str:
    for item in middleware:
        agent_name = getattr(item, "agent_name", None)
        if agent_name:
            return str(agent_name)
    return str(explicit_name or "meta-agent")

__all__ = [
    "GENERAL_PURPOSE_SUBAGENT",
    "artifact_capture_scope",
    "create_deep_agent",
]
