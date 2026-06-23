"""A/B 变体隔离执行器（Phase 7 T4.2，D7=② 子进程隔离）。

职责：给定一个 harness_snapshots.version，解压快照到临时目录，起独立 worker
子进程跑生成，返回 trace_id。

为什么子进程（D7=②）：
  同进程加载多个包版本会 sys.modules 冲突（模块名相同）。子进程天然隔离。
  生产路径（D8=X）同进程跑 current 包，不受影响。

流程：
  1. 从 evolution DB 取 harness_snapshots.tar_blob（HTTP 调 evolution）
  2. 解压到临时目录（唯一，避免并发冲突）
  3. 起 worker 子进程：python -m app.worker.server --package-path <临时目录> --port <n>
  4. POST worker /generate/stream，消费 SSE 流，取 trace_id
  5. worker 进程结束（一次性，跑完即退）

与生产路径的区别：
  - 生产：package_loader.load_current_package()（同进程，固定路径）
  - A/B：load_package_at(临时目录)（子进程，解压快照）
  装配入口统一是 package.assemble(ctx)。

设计依据：设计文档 D7=② + Q9=iii（快照解压临时目录）。
"""
from __future__ import annotations

import logging
import tempfile
import tarfile
import io
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("writer.ab_runner")


@dataclass
class ABRunResult:
    """A/B 单次运行结果。"""
    trace_id: str
    workspace_id: str
    thread_id: str
    status: str  # completed / failed
    error: str | None = None


def fetch_snapshot_tar(evolution_url: str, version: int) -> bytes:
    """从 evolution HTTP 拉 harness_snapshots.tar_blob。

    evolution 暴露 GET /api/snapshots/{version}/tar 端点（Phase 5 提供）。
    """
    import httpx
    resp = httpx.get(
        f"{evolution_url.rstrip('/')}/api/snapshots/{version}/tar",
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.content


def extract_snapshot(tar_blob: bytes) -> Path:
    """解压快照 tar 到临时目录，返回目录路径。

    临时目录用 mkdtemp 保证唯一（并发 A/B 不冲突）。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="harness_ab_"))
    with tarfile.open(fileobj=io.BytesIO(tar_blob), mode="r:gz") as tar:
        tar.extractall(tmp_dir)
    logger.info("A/B 快照解压到: %s", tmp_dir)
    return tmp_dir


def run_ab_variant(
    snapshot_version: int,
    workspace_path: str,
    *,
    evolution_url: str,
    payload: dict,
    workspace_id: str | None = None,
    owner_id: str = "ab-replay",
    worker_port: int = 0,
) -> ABRunResult:
    """跑一个 A/B 变体（解压快照 → 起 worker 子进程 → 跑生成 → 回传结果）。

    Args:
        snapshot_version: harness_snapshots.version（候选快照版本号）
        workspace_path: workspace 绝对路径
        evolution_url: evolution 服务地址（取快照用）
        payload: 生成请求 payload
        workspace_id / owner_id: 归属
        worker_port: worker 监听端口（0 = 自动选）

    Returns:
        ABRunResult（含 trace_id）。

    注意：本函数当前是骨架——实际起子进程 + HTTP 调用需要完整 executor 环境。
    完整实现分两步：解压快照（本模块）+ 子进程编排（需 subprocess + httpx 轮询）。
    """
    # 1. 取快照 tar
    tar_blob = fetch_snapshot_tar(evolution_url, snapshot_version)

    # 2. 解压临时目录
    pkg_dir = extract_snapshot(tar_blob)

    # 3. 起 worker 子进程（骨架：实际部署时用 subprocess.Popen + uvicorn）
    #    python -m app.worker.server --package-path <pkg_dir> --port <worker_port>
    #    然后 POST /generate/stream，消费 SSE 取 trace_id
    #
    # 子进程编排逻辑较长，且依赖执行端完整环境（subprocess + 端口管理 + SSE 客户端），
    # 留到集成测试阶段连通。当前骨架验证"取快照 + 解压"链路。
    logger.info(
        "A/B 变体 v%s：快照已解压到 %s，worker 子进程编排待集成",
        snapshot_version, pkg_dir,
    )

    return ABRunResult(
        trace_id="",
        workspace_id=workspace_id or "",
        thread_id="",
        status="pending_integration",
        error="子进程编排待集成（骨架阶段）",
    )
