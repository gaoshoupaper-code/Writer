"""_fetch_payloads_concurrent —— payload 并发拉取回归测试（NFR-003 / RSK-005）。

验证 EVD-011 的串行 payload 拖慢单轮 refresh 已被根治：
  - 多个 payload 并发拉取，墙钟时间不再随 payload 数线性增长；
  - 单个 payload 失败不阻塞其余；
  - 并发有界（不无限打爆 executor）；
  - 返回内容与串行等价。
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from app.ingestion.ingestion import _PAYLOAD_CONCURRENCY, _fetch_payloads_concurrent


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload or {}


class ConcurrentPayloadFetchTest(unittest.TestCase):
    def test_multiple_payloads_fetched_concurrently(self) -> None:
        """N 个 payload 各 sleep 0.2s：串行需 N×0.2s，并发应远小于此。"""
        payload_ids = {f"pid-{i}" for i in range(6)}

        def fake_get(url, timeout=None, headers=None):
            # 每个 payload 请求模拟 0.2s 网络延迟。
            time.sleep(0.2)
            pid = url.rsplit("/", 1)[-1]
            return _FakeResponse(200, {"id": pid, "data": "x"})

        start = time.perf_counter()
        with patch("httpx.get", side_effect=fake_get):
            result = _fetch_payloads_concurrent("http://exe", "trace-1", payload_ids, None)
        elapsed = time.perf_counter() - start

        # 6 个 payload × 0.2s 串行 = 1.2s；并发（上限 8）应 < 0.6s。
        self.assertEqual(len(result), 6)
        self.assertLess(elapsed, 0.6, f"并发拉取未生效，耗时 {elapsed:.2f}s")

    def test_single_payload_failure_does_not_block_others(self) -> None:
        payload_ids = {"ok-1", "ok-2", "bad-1"}

        def fake_get(url, timeout=None, headers=None):
            pid = url.rsplit("/", 1)[-1]
            if pid == "bad-1":
                return _FakeResponse(500)
            time.sleep(0.1)
            return _FakeResponse(200, {"id": pid})

        with patch("httpx.get", side_effect=fake_get):
            result = _fetch_payloads_concurrent("http://exe", "trace-1", payload_ids, None)

        # 失败的不进结果，成功的两个都进；彼此不阻塞。
        self.assertIn("ok-1", result)
        self.assertIn("ok-2", result)
        self.assertNotIn("bad-1", result)

    def test_payload_exception_does_not_crash_batch(self) -> None:
        payload_ids = {"good", "boom"}

        def fake_get(url, timeout=None, headers=None):
            pid = url.rsplit("/", 1)[-1]
            if pid == "boom":
                raise ConnectionError("executor 暂不可达")
            return _FakeResponse(200, {"id": pid})

        with patch("httpx.get", side_effect=fake_get):
            result = _fetch_payloads_concurrent("http://exe", "trace-1", payload_ids, None)

        # 异常的 payload 被吞掉（记 warning），正常的仍返回。
        self.assertEqual(result, {"good": {"id": "good"}})

    def test_concurrency_is_bounded(self) -> None:
        """同一时刻在途的 httpx.get 数不超过 _PAYLOAD_CONCURRENCY。"""
        payload_ids = {f"pid-{i}" for i in range(_PAYLOAD_CONCURRENCY * 3)}
        in_flight = {"count": 0, "max": 0}
        lock = threading.Lock()

        def fake_get(url, timeout=None, headers=None):
            with lock:
                in_flight["count"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["count"])
            time.sleep(0.05)
            with lock:
                in_flight["count"] -= 1
            return _FakeResponse(200, {"id": url.rsplit("/", 1)[-1]})

        with patch("httpx.get", side_effect=fake_get):
            _fetch_payloads_concurrent("http://exe", "trace-1", payload_ids, None)

        self.assertLessEqual(in_flight["max"], _PAYLOAD_CONCURRENCY)

    def test_empty_set_returns_empty(self) -> None:
        self.assertEqual(_fetch_payloads_concurrent("http://exe", "trace-1", set(), None), {})


if __name__ == "__main__":
    unittest.main()
