"""A/B 候选执行端点（D2 同进程热加载）。

POST /internal/ab/run 的实际执行逻辑：
  1. 准备隔离 workspace（写 demand.md，interview 直通）
  2. importlib 加载候选 source_root（同进程热加载，清理 sys.modules）
  3. assemble(ctx) 装配候选 Agent
  4. 同步跑一次生成（非 SSE），取 trace_id
  5. 存到 _ab_tasks 供 /ab/status 轮询

与生产路径的区别只在 source_root 来源：
  - 生产：load_current_package()（固定 harnesses/repo）
  - A/B：load_package_at(source_root)（snapshot 模式按 commit checkout；working 用 harnesses/repo）
  装配入口统一是 package.assemble(ctx)，单参数契约。

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


# A/B 与生产两条加载路径各自注册的包名。热加载前必须按前缀清掉这两个包
# 及其全部子模块（harness_current_ab.subagents.interview 等），否则第二次 A/B
# import 时 Python 会命中 sys.modules 里第一次的旧模块——旧模块的模块级常量
# PROMPT_PATH（subagents/interview.py）仍指向已被 cleanup_checkout 删除的目录，
# 读 prompt 时 FileNotFoundError。靠 "harnesses"/"current" 字符串匹配清不到这些
# 子模块名，必须按精确包前缀清（与 loader._purge_package_modules 同款做法）。
_AB_PACKAGE_PREFIXES = ("harness_current_ab", "harness_current")


def _clear_package_modules() -> None:
    """清理 sys.modules 中 harness 包的缓存（D11，防同进程版本冲突）。

    第二次热加载前必须清，否则 import 拿到的是第一次的旧缓存。
    """
    keys_to_del = [
        k for k in list(sys.modules)
        if any(k == p or k.startswith(p + ".") for p in _AB_PACKAGE_PREFIXES)
    ]
    for k in keys_to_del:
        del sys.modules[k]
    if keys_to_del:
        logger.info("清理 %d 个 harness 包模块缓存", len(keys_to_del))


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
    source_root: Path,
    demand_md: str,
    trace_recorder,
    writer_settings,
    on_trace_created=None,
    cancel_event: threading.Event | None = None,
    traceparent: str | None = None,
    test_id: str | None = None,
    task_id: str | None = None,
) -> str:
    """跑一次候选生成（同步，非 SSE），返回 trace_id。

    Args:
        source_root: harness 包根目录（snapshot 模式来自 git checkout，
                     working 模式来自 harnesses/repo）
        demand_md: 预置 demand.md 内容
        trace_recorder: TraceRecorder 实例
        writer_settings: writer settings（构建 model 用）
        on_trace_created: 可选回调，trace 创建后立即调用（传 trace_id, workspace_path），
                          供调用方在 running 期间就能拿到 trace_id 做实时展示，
                          并把 workspace_path 登记进主进程 recorder（FR-001 跨进程发现）。
        cancel_event: 可选取消标志；set() 后在下一个 super-step 边界中断生成，
                      trace 收尾为 cancelled（user_stop）。None = 不可取消（原行为）。
        traceparent: 可选 W3C traceparent（FR-004）。继承 evolution 发起链路的上游 context；
                     缺失/非法时由 create_run 生成有效新 context，不阻断运行。
        test_id: 可选 evolution 单次测试 ID（FR-004）。写入 external_refs 供跨服务追溯。
        task_id: 可选 executor 任务 ID（FR-004）。写入 external_refs 供跨服务追溯。

    Returns:
        trace_id
    """
    from contracts.runtime_context import RuntimeContext
    from app.domains.writing.models import build_writer_model
    from app.platform.agent.middleware import TraceMiddleware
    from app.platform.agent.middleware.artifact_capture import EvidenceCaptureError
    from app.platform.agent.runtime import artifact_capture_scope
    # CON-005/FR-003（trace 可见性根治）：复用生产路径的平台级安全边界注入。
    # A/B + 单次测试是 EVD-003 的实际复现入口，必须与生产路径同施加 task 防重放 +
    # 模型重试可观测，否则该入口仍会出现约 18 分钟等待与整任务重放。
    from app.domains.writing.agent import _build_tool_replay_policy, _make_retry_runner_factory
    from app.schemas.screenplay import ThreadSummary
    from datetime import UTC, datetime

    # 1. 准备 workspace
    workspace_path = prepare_ab_workspace(demand_md)
    trace_id = f"trace-{uuid.uuid4().hex}"

    # FR-004：A/B 记忆库 workspace_id（与 memory_backend / trigger_chapter_ingestion 一致）。
    # 提前算出供 finally 清理 memory.db（成功/失败/取消三种终态都需清理，DEC-004）。
    ab_memory_workspace_id = f"{AB_OWNER}_{workspace_path.name}"

    # FR-001：确保 MemoryStorePool 已初始化。A/B 在隔离子进程（spawn）跑，不继承父进程
    # 的已初始化全局（main.py 模块级 init_memory_store_pool 不在子进程 import 链上）。
    # 未初始化时 get_memory_backend 返回 None → 记忆功能静默关闭（FR-001 失效）。
    # 这里幂等初始化：已初始化则跳过，未初始化则用 resolve_memory_root 建池。
    _ensure_memory_pool_initialized()

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
    # FR-004：透传 W3C traceparent（继承 evolution 发起链路）与 test_id/task_id 业务关联，
    # 写入 external_refs 供跨服务追溯；缺失/非法 traceparent 时 create_run 自行生成有效 context。
    extra_refs: dict[str, str] = {}
    if test_id:
        extra_refs["test_id"] = test_id
    if task_id:
        extra_refs["task_id"] = task_id
    trace = trace_recorder.create_run(
        thread,
        "screenplay.ab_run",
        run_purpose="evolution",
        traceparent=traceparent,
        external_refs_extra=extra_refs or None,
    )
    trace_id = trace.trace_id
    from app.platform.agent.runtime_identity import build_runtime_identity

    trace_recorder.set_run_snapshot(
        trace_id,
        build_runtime_identity(harness_root=source_root),
    )
    trace_recorder.append_event(trace_id, {
        "type": "run_meta",
        "status": "running",
        "source": "system",
        "input": {"contract_snapshot": {
            "task_type": "screenplay.ab_run",
            "run_purpose": "evolution",
            "endpoint": "screenplay.ab_run",
            "thread_id": thread.thread_id,
            "workspace_id": thread.workspace_id,
            "session_name": thread.session_name,
            "demand_md": demand_md,
            "demand_available": True,
            "missing": [],
        }},
    })

    # trace 已创建，立即通知调用方（供 running 期间实时展示）。
    # 回传 workspace_path：主进程据此登记进主 recorder，让 /internal/traces/{id}
    # 能查到子进程产物（FR-001 跨进程发现根因修复）。
    if on_trace_created:
        try:
            on_trace_created(trace_id, str(workspace_path))
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

        # FR-001/NWM：A/B 接入记忆读取侧——注入 memory_backend + memory_recall_middleware_cls，
        # 与生产路径对齐。memory_backend 按 A/B 临时 workspace_id 隔离（EVD-005，
        # workspace_id=owner_workspace_name，与生产库不同名，物理隔离）。
        # memory_recall_middleware_cls 从当前候选包加载（而非 load_current_package 的生产缓存包），
        # 保证同进程多版本热加载时装配的是该候选版本的中间件（FR-003）。
        # pool 未初始化时 get_memory_backend 返回 None → 走全量注入（与生产路径降级语义一致）。
        from app.platform.memory import get_memory_backend
        from app.domains.writing.agent import _get_memory_recall_cls

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
            # CON-005/FR-003：与生产路径同注入 task 防重放 + 模型重试可观测边界,
            # 覆盖单次测试/A·B（EVD-003 实际复现入口）。
            tool_replay_policy=_build_tool_replay_policy(trace_recorder, trace_id),
            writer_retry_runner_factory=_make_retry_runner_factory(),
            # FR-001：记忆读取侧接入。激活召回中间件 + quality_callback 埋点（写 trace run_meta）。
            memory_backend=get_memory_backend(f"{AB_OWNER}_{workspace_path.name}"),
            memory_recall_middleware_cls=_get_memory_recall_cls(pkg),
        )

        # 5. assemble（单参数契约，与生产路径 agent.py 一致）
        with artifact_capture_scope(
            recorder=trace_recorder,
            trace_id=trace_id,
            workspace_root=workspace_path,
            strict=True,
        ):
            agent = pkg.assemble(ctx)

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
            "recursion_limit": 300,
        }
        # FR-002 写入侧：A/B 用裸 stream（非 astream_events），拿不到 task 事件，
        # 在每个 super-step 边界扫描 chapter/ 目录检测新完成章节并触发逐章抽取入库。
        # 与生产路径复用同一 trigger_chapter_ingestion（DEC-002 抽触发器方案），不破坏
        # 逐 super-step 取消能力（CON-001：抽取在 super-step 边界同步执行，取消时已完成的
        # 章节已抽取、未完成的不触发——EDGE-001）。
        from app.domains.writing.events import scan_extracted_chapters, trigger_chapter_ingestion

        extracted_chapters: set[int] = set()
        cancelled = False
        for _chunk in agent.stream(agent_input, config=run_config):
            # FR-002：检测本次 super-step 新写盘的章节，触发逐章抽取入库（Causal Publish Flow）。
            for ch_idx in sorted(scan_extracted_chapters(workspace_path, extracted_chapters)):
                trigger_chapter_ingestion(workspace_path, ab_memory_workspace_id, ch_idx)
                extracted_chapters.add(ch_idx)
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

    except EvidenceCaptureError as exc:
        logger.exception("A/B 生成取证失败: trace_id=%s", trace_id)
        trace_recorder.fail_evidence_capture_run(thread, trace_id, exc)
        raise
    except BaseException as exc:
        logger.exception("A/B 生成失败: trace_id=%s", trace_id)
        trace_recorder.fail_run(thread, trace_id, exc)
        raise
    finally:
        # FR-004/DEC-004：A/B run 终态（成功/失败/取消）清理临时 memory.db（含 -wal/-shm）。
        # 进化复盘完全依赖 trace 的 memory_quality run_meta 事件（CON-003），memory.db 是
        # 执行期临时工作记忆，清理不影响进化。pool 未初始化（记忆关闭）时跳过。生产 memory.db
        # 不受影响（workspace 命名隔离，EVD-005）。清理失败记日志不阻断（memory.db 在临时目录）。
        _cleanup_ab_memory_db(ab_memory_workspace_id)


def _cleanup_ab_memory_db(workspace_id: str) -> None:
    """FR-004：清理 A/B 临时 memory.db（成功/失败/取消终态统一调用）。

    通过 MemoryStorePool.drop_sync 关闭可能缓存的 store 并删除 db 文件（含 -wal/-shm）。
    pool 未初始化（记忆功能关闭）或 db 不存在时静默跳过。失败记日志不阻断
    （memory.db 在临时目录，最终随容器/系统清理——RSK-004 兜底）。
    """
    try:
        from app.platform.memory.store import get_memory_store_pool
        pool = get_memory_store_pool()
        pool.drop_sync(workspace_id)
    except RuntimeError:
        # MemoryStorePool 未初始化（记忆系统关闭）——无 db 可清。
        pass
    except Exception:
        logger.debug("A/B memory.db 清理失败（workspace_id=%s）", workspace_id, exc_info=True)


def _ensure_memory_pool_initialized() -> None:
    """FR-001：幂等初始化 MemoryStorePool（A/B 子进程入口）。

    A/B 在 spawn 子进程跑（worker_process._worker_main），子进程的 import 链不经过
    executor/app/main.py（那里有模块级 init_memory_store_pool），导致子进程的 pool 单例为 None。
    get_memory_backend 检测到 None 时返回 None，记忆功能静默关闭——FR-001 失效。

    本函数在 run_ab_generation 入口幂等初始化：已初始化则跳过（get_memory_store_pool 不抛），
    未初始化则用 resolve_memory_root 建池（与 main.py 一致的 root 解析逻辑）。
    """
    try:
        from app.platform.memory.store import (
            MemoryStorePool,
            get_memory_store_pool,
            init_memory_store_pool,
            resolve_memory_root,
        )
        try:
            get_memory_store_pool()  # 已初始化则不抛
            return
        except RuntimeError:
            pass  # 未初始化，继续建池
        init_memory_store_pool(MemoryStorePool(resolve_memory_root()))
        logger.info("A/B 子进程 MemoryStorePool 已初始化")
    except Exception:
        # 记忆系统初始化失败不阻断 A/B 创作（降级为无记忆，与生产路径降级语义一致）。
        logger.warning("A/B 子进程 MemoryStorePool 初始化失败，记忆系统将关闭", exc_info=True)


__all__ = [
    "prepare_ab_workspace",
    "load_package_at",
    "run_ab_generation",
    "AB_OWNER",
]
