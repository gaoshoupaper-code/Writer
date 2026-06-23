"""worker HTTP 服务实现（Phase 7 包化重构，D7=② 子进程隔离）。

worker 进程 = A/B 回放/变体的隔离执行环境。给定一个 Agent 包路径，
worker 能隔离地跑一次生成并吐 trace。

架构定位（Phase 7）：
  生产路径（D8=X）：executor 同进程 import current 包，不走 worker。
  A/B/回放路径（D7=②）：每个变体起独立 worker 子进程，指向解压后的临时包目录。
  子进程隔离天然解决 sys.modules 冲突（同进程跑多个包版本会撞模块名）。

  worker 与生产路径的差异只在"包从哪来"：
  - 生产：package_loader.load_current_package()（固定 evolution/harnesses/current/）
  - worker：按 --package-path 加载（current 或临时解压目录）
  装配入口统一是 package.assemble(ctx)，逻辑无差异。

设计依据：设计文档 D7=②（子进程隔离）+ D8=X（生产同进程，仅 A/B 子进程）。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("worker")


# ── worker FastAPI 应用 ─────────────────────────────────────


class GenerateRequest(BaseModel):
    """worker 生成请求（执行端/evolution 转发）。

    含装配所需全部请求级信息 + 生成 payload。
    worker 进程启动时已锁定包路径（--package-path），请求里不带包信息。
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
    package_path: str | None = None,
) -> FastAPI:
    """创建 worker FastAPI 应用（Phase 7 包化）。

    Args:
        generate_fn: 生成函数（async generator），签名：
            (req: GenerateRequest) -> AsyncIterator[dict]
            实际部署时注入 MetaAgentService.generate_stream（含 SSE 编排、trace、checkpoint）。
            None 时 /generate/stream 返回 501（骨架降级，供测试）。
        package_path: worker 锁定的包路径（current 或临时解压目录）。
            None = 用 settings.harness_package_path（默认 current）。
    """
    app = FastAPI(title="self-harness worker (Phase 7)")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "package_path": package_path,
            "generate_ready": generate_fn is not None,
        }

    @app.post("/generate/stream")
    async def generate_stream(req: GenerateRequest) -> StreamingResponse:
        """生成端点：委托 generate_fn 跑生成 + SSE 透传。

        generate_fn 由 MetaAgentService 注入，内部走包装配
        （package_loader → package.assemble(ctx) → create_deep_agent）。
        """
        from fastapi import HTTPException
        if generate_fn is None:
            raise HTTPException(
                status_code=501,
                detail="worker 未注入 generate_fn（部署时注入 MetaAgentService.generate_stream）",
            )
        async def stream():
            async for event in generate_fn(req):
                import json
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def load_package_at(package_path: str | Path):
    """加载指定路径的 Agent 包（worker 启动时调一次）。

    与 package_loader.load_current_package 的区别：
    - load_current_package：固定读 settings.harness_package_path，模块名 harness_current
    - 本函数：按传入路径加载，模块名带路径哈希避免冲突（子进程内单包，不会撞）

    用 importlib + submodule_search_locations 让包内相对 import 生效（D8 验证过的机制）。
    """
    import importlib.util
    import sys

    pkg_path = Path(package_path).resolve()
    init_path = pkg_path / "__init__.py"
    if not init_path.exists():
        raise FileNotFoundError(f"Agent 包不存在: {init_path}")

    # 模块名用目录名（子进程内只有一个包，不会冲突）
    mod_name = f"harness_worker_{pkg_path.name}"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        init_path,
        submodule_search_locations=[str(pkg_path)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法创建包加载 spec: {init_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_worker(
    port: int,
    *,
    package_path: str | None = None,
    generate_fn: Any | None = None,
) -> None:
    """启动 worker 进程（Phase 7 包化）。

    流程：
      1. 确定包路径（传入或默认 current）
      2. 加载包（验证 assemble 可用）
      3. 注入 generate_fn（MetaAgentService.generate_stream）
      4. 启动 uvicorn

    Args:
        port: 监听端口
        package_path: 包路径（current 或临时解压目录）。None = current。
        generate_fn: 生成函数（注入）。None 时尝试从 MetaAgentService 构造；
                     构造失败则 /generate/stream 返回 501（降级）。
    """
    import uvicorn
    from app.platform.core.settings import get_settings

    # 1. 确定包路径
    if package_path is None:
        s = get_settings()
        package_path = s.harness_package_path
        # 相对路径基于项目根
        p = Path(package_path)
        if not p.is_absolute():
            package_path = str(Path(__file__).resolve().parents[3] / p)

    # 2. 加载包（验证 assemble 可用，worker 启动即暴露包加载错误）
    pkg = load_package_at(package_path)
    if not hasattr(pkg, "assemble"):
        raise RuntimeError(f"包无 assemble 函数: {package_path}")
    logger.info("worker 启动：包 %s（assemble 可用）", package_path)

    # 3. generate_fn：注入优先，否则尝试构造 MetaAgentService
    if generate_fn is None:
        generate_fn = _try_build_generate_fn()

    app = create_worker_app(generate_fn, package_path=str(package_path))
    uvicorn.run(app, host="0.0.0.0", port=port)


def _try_build_generate_fn() -> Any | None:
    """尝试从 MetaAgentService 构造 generate_fn（部署时用）。

    MetaAgentService 构造依赖 trace_recorder/checkpointer/style_store 等，
    这些在完整 executor 环境可用。测试/骨架环境返回 None（/generate/stream 501）。
    """
    try:
        from app.routers.context import get_agent_service
        service = get_agent_service()
        # worker 的 generate_fn 走 _assemble_via_package（Phase 7 包化）
        async def generate_fn(req: GenerateRequest):
            from pathlib import Path
            async for event in service.generate_stream(
                workspace_path=Path(req.workspace_path),
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
    parser = argparse.ArgumentParser(description="self-harness worker (Phase 7)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--package-path", type=str, default=None,
                        help="Agent 包路径（current 或临时解压目录）。默认 current。")
    args = parser.parse_args()
    run_worker(args.port, package_path=args.package_path)
