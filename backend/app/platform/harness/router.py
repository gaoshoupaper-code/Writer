"""harness 版本路由层（Phase 2 T2.3，S5 进程隔离 + S6 HTTP/SSE）。

职责：执行端收到生成请求 → 决定路由到哪个 harness 版本的 worker。
路由决策是纯逻辑（按 label/版本查 mapping），实际 HTTP 转发由调用方执行。

worker 地址解析：版本号 → worker 进程的 host:port。
两种模式：
  - 单机模式（开发）：所有 worker 在本机，端口 = BASE_PORT + version
  - 编排模式（生产）：版本→地址 mapping 由 docker-compose/编排工具维护

注意：本模块是路由决策层，不直接发 HTTP 请求（解耦，便于测试）。
实际转发（httpx 调 worker + SSE 透传）在 routers 层接通。

⚠️ 完整 HTTP 转发 + SSE 透传需 worker 部署后真机验证。

设计依据：设计文档 S5/S6/C3（多版本进程管理）/C6（容器网络）。
"""
from __future__ import annotations

from dataclasses import dataclass

# worker 端口基址（单机模式：version N 的 worker 在 BASE_PORT + N）
# 生产环境应改为从编排工具读 mapping
WORKER_BASE_PORT = 9000
WORKER_HOST = "127.0.0.1"  # 单机默认；容器化时改为服务名


@dataclass
class WorkerAddress:
    """worker 进程的网络地址。"""
    host: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def resolve_worker_address(version: int, *, host: str | None = None) -> WorkerAddress:
    """解析 harness 版本对应的 worker 地址（单机模式：BASE_PORT + version）。

    生产/容器化时应替换为从编排工具读 version→address mapping。

    Args:
        version: harness 版本号
        host: worker 主机（默认本机）
    """
    return WorkerAddress(
        host=host or WORKER_HOST,
        port=WORKER_BASE_PORT + version,
    )


def resolve_production_worker(harness_repo_url: str | None = None) -> WorkerAddress | None:
    """解析当前 production harness 版本的 worker 地址。

    ⚠️ 完整实现需查 monitoring 的 harness_repo（按 production label 取版本）。
    本骨架返回 None（表示未配置路由，走本地直接装配）。

    实际接入：调 monitoring API GET /api/harnesses?label=production → 版本 → resolve_worker_address。
    """
    # TODO（worker 部署后）：查 monitoring 取 production 版本
    # from app import httpx; resp = httpx.get(f"{harness_repo_url}/api/harnesses?label=production")
    # version = resp.json()["version"]; return resolve_worker_address(version)
    return None
