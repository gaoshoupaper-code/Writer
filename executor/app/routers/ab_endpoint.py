"""A/B 候选执行端点（补全骨架，D2 同进程热加载）。

POST /internal/ab/run 的实际执行逻辑：
  1. 准备隔离 workspace（写 demand.md，interview 直通）
  2. importlib 加载候选 source_root（同进程热加载，清理 sys.modules）
  3. assemble(ctx, config, source_root) 装配候选 Agent
  4. 同步跑一次生成（非 SSE），取 trace_id
  5. 存到 _ab_tasks 供 /ab/status 轮询

与生产路径的区别：
  - 生产：MetaAgentService → load_current_package() → assemble(ctx)（无 config，硬编码）
  - A/B：本模块 → load_package_at(source_root) → assemble(ctx, config, source_root)
  装配入口统一是 package.assemble，只是是否传 config。

设计依据：.claude/md/20260627_135113_进化端单Agent设计.md（D2 同进程热加载）
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("writer.ab_endpoint")

# A/B 专用 owner（不污染用户数据）
AB_OWNER = "ab-evolve"


def prepare_ab_workspace(demand_md: str) -> Path:
    """准备一个隔离的 A/B workspace，写入 demand.md（interview 直通用）。

    Args:
        demand_md: 预置的 demand.md 内容（从评估集来）

    Returns:
        workspace 绝对路径
    """
    ws = Path(tempfile.mkdtemp(prefix="ab_ws_"))
    # demand.md 带 confirmed 状态（DemandPreloadMiddleware 据此跳过 interview）
    # 如果 demand_md 元信息里没有 status，补一个
    if "status:" not in demand_md[:300]:
        demand_md = (
            "<!--\n元信息：\n- status: confirmed\n- mode: auto\n"
            f"<!--\n{demand_md}"
        )
    elif "status: confirmed" not in demand_md[:300]:
        # 已有元信息但不是 confirmed，强制改 confirmed（评估集直通）
        import re
        demand_md = re.sub(
            r"status:\s*\w+", "status: confirmed", demand_md[:300], count=1
        ) + demand_md[300:]
    (ws / "demand.md").write_text(demand_md, encoding="utf-8")
    logger.info("A/B workspace 准备好: %s（demand.md %d 字符）", ws, len(demand_md))
    return ws


def _clear_package_modules() -> None:
    """清理 sys.modules 中 harnesses 包的缓存（D11，防同进程版本冲突）。

    第二次热加载前必须清，否则 import 拿到的是第一次的旧缓存。
    """
    keys_to_del = [
        k for k in list(sys.modules)
        if "harnesses" in k or k.endswith("current")
    ]
    for k in keys_to_del:
        del sys.modules[k]
    if keys_to_del:
        logger.info("清理 %d 个 harnesses 包模块缓存", len(keys_to_del))


def load_package_at(source_root: Path):
    """importlib 加载指定 source_root 的 harness 包。

    source_root = evolution/harnesses/current/（生产或候选改动后的目录）。
    """
    import importlib.util

    _clear_package_modules()

    init_file = source_root / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"harness 包入口不存在: {init_file}")

    spec = importlib.util.spec_from_file_location(
        "harness_current_ab", init_file, submodule_search_locations=[str(source_root)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 harness 包: {source_root}")
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["harness_current_ab"] = pkg
    spec.loader.exec_module(pkg)
    logger.info("harness 包已加载: %s", source_root)
    return pkg


def run_ab_generation(
    *,
    config: dict | None,
    source_root: Path,
    demand_md: str,
    trace_recorder,
    writer_settings,
    on_trace_created=None,
    cancel_event: threading.Event | None = None,
) -> str:
    """跑一次候选生成（同步，非 SSE），返回 trace_id。

    Args:
        config: 候选 HarnessConfig（None = 用硬编码 assemble）
        source_root: harness 包根目录
        demand_md: 预置 demand.md 内容
        trace_recorder: TraceRecorder 实例
        writer_settings: writer settings（构建 model 用）
        on_trace_created: 可选回调，trace 创建后立即调用（传 trace_id），
                          供调用方在 running 期间就能拿到 trace_id 做实时展示。
        cancel_event: 可选取消标志；set() 后在下一个 super-step 边界中断生成，
                      trace 收尾为 cancelled（user_stop）。None = 不可取消（原行为）。

    Returns:
        trace_id
    """
    from contracts.runtime_context import RuntimeContext
    from app.domains.writing.models import build_writer_model
    from app.platform.agent.middleware import TraceMiddleware
    from app.schemas.screenplay import ThreadSummary
    from datetime import UTC, datetime

    # 1. 准备 workspace
    workspace_path = prepare_ab_workspace(demand_md)
    trace_id = f"trace-{uuid.uuid4().hex}"

    # 2. 构建 thread summary（A/B 用虚拟 thread）
    now = datetime.now(UTC).isoformat()
    thread = ThreadSummary(
        thread_id=f"ab-{uuid.uuid4().hex[:8]}",
        workspace_id=f"ab-ws-{uuid.uuid4().hex[:8]}",
        session_name="evolve-ab",
        workspace_path=str(workspace_path),
        created_at=now,
        updated_at=now,
        user_id=AB_OWNER,
    )

    # 3. 创建 trace run
    trace = trace_recorder.create_run(
        thread, "screenplay.ab_run", run_purpose="evolution"
    )
    trace_id = trace.trace_id

    # trace 已创建，立即通知调用方（供 running 期间实时展示）
    if on_trace_created:
        try:
            on_trace_created(trace_id)
        except Exception:
            pass

    try:
        # 4. 加载包 + 构建 ctx
        pkg = load_package_at(source_root)

        # backend 必须绑定到 A/B 临时 workspace（与生产路径 base_service 一致），
        # 否则 MetaAgent 的文件操作工具在错误的根目录找 demand.md，找不到。
        from app.platform.agent.runtime import FilesystemBackend

        backend = FilesystemBackend(root_dir=workspace_path, virtual_mode=True)
        model = build_writer_model(writer_settings)

        ctx = RuntimeContext(
            model=model,
            backend=backend,
            checkpointer=None,  # A/B 不需要 checkpoint 恢复
            workspace_path=workspace_path,
            trace_id=trace_id,
            owner_id=AB_OWNER,
            styles=None,  # A/B 用裸 prompt
            trace_recorder=trace_recorder,
            trace_middleware_cls=TraceMiddleware,
        )

        # 5. assemble（config 驱动）
        agent = pkg.assemble(ctx, config, source_root)

        # 6. 构造输入（简单 prompt，interview 直通后会进 storybuilding）
        user_prompt = (
            "请根据 workspace 中的 demand.md 开始创作。"
            "demand.md 已确认，直接进入故事构建。"
        )
        agent_input = {"messages": [{"role": "user", "content": user_prompt}]}

        # 7. 同步跑：用 stream() 逐 super-step 迭代，便于在边界检查取消标志。
        # （原 invoke() 是全阻塞，无法中途停止；stream 每轮 yield 一个 super-step）
        logger.info("A/B 生成启动: trace_id=%s source=%s", trace_id, source_root)
        run_config = {
            "configurable": {"thread_id": thread.thread_id},
            "recursion_limit": 200,
        }
        cancelled = False
        for _chunk in agent.stream(agent_input, config=run_config):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break

        if cancelled:
            # 用户在边界处停止：trace 收尾为 cancelled，已生成的部分保留
            trace_recorder.cancel_run(thread, trace_id, reason="user_stop")
            logger.info("A/B 生成被用户停止: trace_id=%s", trace_id)
            return trace_id

        # 8. 完成 trace
        trace_recorder.complete_run(thread, trace_id)
        logger.info("A/B 生成完成: trace_id=%s", trace_id)
        return trace_id

    except BaseException as exc:
        logger.exception("A/B 生成失败: trace_id=%s", trace_id)
        trace_recorder.fail_run(thread, trace_id, exc)
        raise


__all__ = [
    "prepare_ab_workspace",
    "load_package_at",
    "run_ab_generation",
    "AB_OWNER",
]
