"""Phase 2 T2.3：harness 版本路由层测试。

覆盖：
- resolve_worker_address：版本号 → 端口（单机模式 BASE_PORT + version）
- WorkerAddress.base_url 拼接
- resolve_production_worker：未配置时返回 None
"""
from app.platform.harness.router import (
    WORKER_BASE_PORT,
    WorkerAddress,
    resolve_production_worker,
    resolve_worker_address,
)


class TestResolveWorkerAddress:
    def test_single_machine_port_offset(self) -> None:
        """单机模式：版本 N 的 worker 在 BASE_PORT + N。"""
        addr = resolve_worker_address(1)
        assert addr.port == WORKER_BASE_PORT + 1

        addr5 = resolve_worker_address(5)
        assert addr5.port == WORKER_BASE_PORT + 5

    def test_default_host_localhost(self) -> None:
        addr = resolve_worker_address(1)
        assert addr.host == "127.0.0.1"

    def test_custom_host(self) -> None:
        addr = resolve_worker_address(1, host="worker-svc")
        assert addr.host == "worker-svc"

    def test_base_url_format(self) -> None:
        addr = WorkerAddress(host="h", port=1234)
        assert addr.base_url == "http://h:1234"


class TestResolveProductionWorker:
    def test_returns_none_when_not_configured(self) -> None:
        """未接入 monitoring 时返回 None（走本地装配）。"""
        assert resolve_production_worker() is None
        assert resolve_production_worker("http://monitoring:7789") is None
