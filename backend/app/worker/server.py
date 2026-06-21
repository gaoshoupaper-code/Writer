"""worker HTTP 服务实现（Phase 2 T2.1）。

worker 进程入口：python -m app.worker.server --port <n> --harness-version <id>

关键职责：
  1. 动态加载指定 harness 版本（importlib 加载 harnesses/<id>/harness.py）
  2. 收到生成请求 → 用该 harness 请求级装配 agent → 跑 generate_stream → SSE 透传

注意：worker 与主 backend 共享 settings/workspace/checkpointer 等基础设施
（同机部署时）。容器化时通过 volume 挂载共享 workspace + harnesses 代码。
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("worker")


# ── harness 动态加载（D2 代码定义）──


class HarnessLoadError(Exception):
    """harness 代码加载失败（语法错/契约违反/缺类）。"""


def load_harness_instance(code_path: str | Path) -> Any:
    """动态加载 harness.py，返回 WriterHarness 子类实例。

    importlib 加载文件 → 找 WriterHarness 子类 → 实例化。
    多个子类时取最后一个定义的（约定：harness.py 只定义一个 WriterHarness）。

    Raises:
        HarnessLoadError: 文件不存在/语法错/无 WriterHarness 子类。
    """
    from app.platform.harness import WriterHarness  # noqa: F401（基类引用）

    path = Path(code_path)
    if not path.exists():
        raise HarnessLoadError(f"harness 代码文件不存在: {path}")

    spec = importlib.util.spec_from_file_location(f"harness_{path.parent.name}", path)
    if spec is None or spec.loader is None:
        raise HarnessLoadError(f"无法加载 harness 模块: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise HarnessLoadError(f"harness 代码执行失败: {exc}") from exc

    # 找 WriterHarness 子类
    harness_classes = [
        obj for name, obj in vars(module).items()
        if isinstance(obj, type)
        and obj.__name__ != "WriterHarness"
        and _is_writer_harness_subclass(obj)
    ]
    if not harness_classes:
        raise HarnessLoadError(
            f"harness.py 未定义 WriterHarness 子类（文件: {path}）"
        )
    harness_cls = harness_classes[-1]  # 取最后定义的
    return harness_cls()


def _is_writer_harness_subclass(obj: type) -> bool:
    """判断 obj 是否是 WriterHarness 的子类（延迟 import 避免循环）。"""
    from app.platform.harness import WriterHarness
    try:
        return issubclass(obj, WriterHarness)
    except TypeError:
        return False


# ── worker FastAPI 应用 ─────────────────────────────────────


class GenerateRequest(BaseModel):
    """worker 生成请求（执行端转发）。

    含装配所需全部请求级信息（workspace/trace/owner/style + 生成 payload）。
    """
    workspace_path: str
    workspace_id: str | None = None
    trace_id: str | None = None
    owner_id: str | None = None
    thread_id: str | None = None
    payload: dict[str, Any]
    run_purpose: str = "user_generation"


def create_worker_app(harness_instance: Any) -> FastAPI:
    """创建 worker FastAPI 应用。

    Args:
        harness_instance: 已加载的 WriterHarness 实例（启动时加载一次）。
    """
    app = FastAPI(title="self-harness worker")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "harness_id": harness_instance.harness_id(),
        }

    @app.post("/generate/stream")
    async def generate_stream(req: GenerateRequest) -> StreamingResponse:
        """生成端点：harness 装配 agent → 跑 → SSE 透传。

        注意：完整实现需要 access MetaAgentService 的 generate_stream（含 SSE 编排、
        trace 记录、checkpoint）。本骨架委托给一个可注入的 generate 函数，
        实际部署时注入 MetaAgentService.generate_stream。

        TODO（T2.2 容器化时接通）：注入真实 generate 函数。
        """
        # 占位：实际接入 MetaAgentService 后实现
        # 目前返回 501，标明需接入生成链路
        from fastapi import HTTPException
        raise HTTPException(
            status_code=501,
            detail="worker 生成端点待接入 MetaAgentService（T2.2 容器化时完成）",
        )

    return app


def run_worker(port: int, harness_version: int, harnesses_root: str) -> None:
    """启动 worker 进程。

    Args:
        port: 监听端口
        harness_version: 要加载的 harness 版本号
        harnesses_root: harness 代码根目录（harnesses/<version>/harness.py）
    """
    import uvicorn

    code_path = Path(harnesses_root) / str(harness_version) / "harness.py"
    logger.info("worker 启动：加载 harness v%s from %s", harness_version, code_path)
    harness = load_harness_instance(code_path)
    logger.info("harness 加载成功：id=%s", harness.harness_id())

    app = create_worker_app(harness)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="self-harness worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--harness-version", type=int, required=True)
    parser.add_argument("--harnesses-root", default="harnesses")
    args = parser.parse_args()
    run_worker(args.port, args.harness_version, args.harnesses_root)
