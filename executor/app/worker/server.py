"""worker HTTP 服务实现（Phase 2 T2.1）。

worker 进程入口：python -m app.worker.server --port <n> --harness-version <id>

关键职责：
  1. 动态加载指定 harness 版本（importlib 加载 harnesses/<id>/harness.py）
  2. 收到生成请求 → 用该 harness 请求级装配 agent → 跑 generate_stream → SSE 透传

架构定位（Phase 4 重构澄清）：
  worker 是 executor 对外暴露的「隔离执行能力」——给定一个 harness 版本，
  executor 能隔离地跑一次生成并吐 trace。evolution 是这个能力的调用方之一
  （通过 HTTP 调 /generate/stream 或 /internal/ab-replay），但 worker 不依赖
  evolution，executor 也不感知 evolution 是否在线。

  边界划分：
  - harnesses/ 代码（定义生成流程怎么装配）→ 归 executor
  - harness 版本管理（版本记录/label/批准上线）→ 归 evolution
  - worker 进程（执行能力）→ 归 executor

当前状态：骨架阶段。/generate/stream 返回 501（待接通 MetaAgentService）。
executor 生产路径目前用写死的 harnesses/v1（meta/agent.py 硬编码 import），
不走 worker 动态加载。

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


def create_worker_app(
    generate_fn: Any | None = None,
    *,
    manifest_version: int | None = None,
) -> FastAPI:
    """创建 worker FastAPI 应用（Phase 6 T4.6，manifest 体系）。

    Args:
        generate_fn: 生成函数（async generator），签名：
            (req: GenerateRequest, manifest_version: int | None) -> AsyncIterator[dict]
            实际部署时注入 MetaAgentService.generate_stream（含 SSE 编排、trace、checkpoint）。
            None 时 /generate/stream 返回 501（骨架降级，供测试）。
        manifest_version: 固定使用的 manifest 版本（A/B 回放历史版本用）。
            None = 用当前 production manifest。

    架构定位（Phase 6）：worker 不再加载静态 harness.py，而是通过 MetaAgentService
    的 generate_fn 走 manifest 装配（_assemble_via_manifest）。worker 进程启动时
    preload C 类 surface（D11），由 MetaAgentService 负责完整装配。
    """
    app = FastAPI(title="self-harness worker")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "manifest_version": manifest_version,
            "generate_ready": generate_fn is not None,
        }

    @app.post("/generate/stream")
    async def generate_stream(req: GenerateRequest) -> StreamingResponse:
        """生成端点：委托 generate_fn 跑生成 + SSE 透传。

        generate_fn 由 MetaAgentService 注入，内部走 manifest 装配
        （_assemble_via_manifest → manifest_loader.assemble → create_deep_agent）。
        """
        from fastapi import HTTPException
        if generate_fn is None:
            raise HTTPException(
                status_code=501,
                detail="worker 未注入 generate_fn（部署时注入 MetaAgentService.generate_stream）",
            )
        # SSE 透传 generate_fn 的产出
        async def stream():
            async for event in generate_fn(req, manifest_version):
                import json
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def run_worker(
    port: int,
    *,
    manifest_version: int | None = None,
    generate_fn: Any | None = None,
) -> None:
    """启动 worker 进程（Phase 6 T4.6，manifest 体系）。

    不再加载静态 harness.py，改为：
      1. 拉 production manifest（或指定版本）
      2. preload C 类 surface（D11 进程启动加载）
      3. 注入 generate_fn（MetaAgentService.generate_stream）
      4. 启动 uvicorn

    Args:
        port: 监听端口
        manifest_version: 固定 manifest 版本（A/B 回放历史用）。None = production。
        generate_fn: 生成函数（注入）。None 时尝试从 MetaAgentService 构造；
                     构造失败则 /generate/stream 返回 501（降级）。
    """
    import uvicorn
    from app.platform.harness.manifest_loader import get_loader, preload_c_surfaces

    # 1. 拉 manifest + preload C 类（D11）
    loader = get_loader()
    manifest = loader.fetch_production() if manifest_version is None else loader.fetch_by_version(manifest_version)
    if manifest is None:
        logger.error("worker 启动失败：无法拉取 manifest（evolution 不可用且无缓存）")
        raise RuntimeError("无法拉取 manifest，worker 无法启动")
    c_pool = preload_c_surfaces(manifest["entries"])
    logger.info(
        "worker 启动：manifest v%s，预加载 %d 个 C 类 surface",
        manifest["manifest_version"], len(c_pool),
    )

    # 2. generate_fn：注入优先，否则尝试构造 MetaAgentService
    if generate_fn is None:
        generate_fn = _try_build_generate_fn()

    app = create_worker_app(generate_fn, manifest_version=manifest["manifest_version"])
    uvicorn.run(app, host="0.0.0.0", port=port)


def _try_build_generate_fn() -> Any | None:
    """尝试从 MetaAgentService 构造 generate_fn（部署时用）。

    MetaAgentService 构造依赖 trace_recorder/checkpointer/style_store 等，
    这些在完整 executor 环境可用。测试/骨架环境返回 None（/generate/stream 501）。
    """
    try:
        from app.routers.context import get_agent_service
        service = get_agent_service()
        # 包装成 generate_fn(req, manifest_version) 签名
        async def generate_fn(req, manifest_version):
            # service.generate_stream 内部走 _assemble_via_manifest（开关 writer_use_manifest）
            async for event in service.generate_stream(
                workspace_path=req.workspace_path,
                workspace_id=req.workspace_id,
                trace_id=req.trace_id,
                owner_id=req.owner_id,
                thread_id=req.thread_id,
                payload=req.payload,
                run_purpose=req.run_purpose,
            ):
                yield event
        logger.info("generate_fn 从 MetaAgentService 构造成功")
        return generate_fn
    except Exception:
        logger.warning("MetaAgentService 构造失败，/generate/stream 将返回 501", exc_info=True)
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="self-harness worker")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--manifest-version", type=int, default=None,
                        help="固定 manifest 版本（默认 production）")
    args = parser.parse_args()
    run_worker(args.port, manifest_version=args.manifest_version)
