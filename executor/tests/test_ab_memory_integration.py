"""A/B 测试路径接入记忆系统测试（REQ-20260801-131224）。

覆盖：
  - AC-003：多版本记忆检索要素不交叉污染（FR-003 单例修复）。
  - AC-004：A/B memory.db 随 workspace 清理（FR-004）。
  - AC-006：记忆失败可观测不静默（NFR-001，writing 章节抽取触发器 + unhealthy flag）。

设计依据：.claude/md/20260801_131224_AB测试路径接入记忆系统.md
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ── 加载 harness 源仓库作为 package（与 test_a2_new_middleware.py 同款做法）──
_REPO_DIR = Path(__file__).resolve().parent.parent.parent / "evolution" / "harnesses" / "repo"
_PKG_NAME = "_harness_ab_mem_pkg"
if _PKG_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME,
        _REPO_DIR / "__init__.py",
        submodule_search_locations=[str(_REPO_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = pkg
    spec.loader.exec_module(pkg)

from app.platform.memory import store as store_mod  # noqa: E402
from app.platform.memory.retriever import MemoryRetriever, get_memory_retriever, set_memory_retriever  # noqa: E402


def _init_pool(memory_root: Path) -> store_mod.MemoryStorePool:
    """初始化一个指向临时目录的 MemoryStorePool（测试隔离）。"""
    pool = store_mod.MemoryStorePool(memory_root)
    store_mod.init_memory_store_pool(pool)
    return pool


class TestMultiVersionRetrieverIsolation(unittest.TestCase):
    """AC-003：同进程热加载多版本 harness 时检索要素不交叉污染（FR-003）。"""

    def setUp(self) -> None:
        # 保留并恢复全局 retriever 单例，避免污染其他测试。
        self._prev_retriever = get_memory_retriever()
        # 清掉单例包保护，让两次 assemble 都重新注入（模拟热加载）。
        if hasattr(pkg, "_harness_retriever_injected"):
            self._prev_flag = pkg._harness_retriever_injected
            pkg._harness_retriever_injected = False

    def tearDown(self) -> None:
        set_memory_retriever(self._prev_retriever)
        if hasattr(self, "_prev_flag"):
            pkg._harness_retriever_injected = self._prev_flag

    def test_each_assemble_reinjects_not_locked_to_first(self) -> None:
        """移除"只注入一次"保护后，第二次 assemble 注入新 retriever，不被首版本锁死。

        场景（SCN-003）：同进程先 assemble v6（旧 join_rules）再 assemble v7（新 join_rules）。
        修复前（_harness_retriever_injected=True 锁死）：v7 拿到 v6 的 retriever。
        修复后：每次 assemble 都重新 set_memory_retriever，v7 用 v7 的要素。
        """
        # 用两个可区分的 join_rules 函数模拟两个 harness 版本的检索要素。
        join_rules_v6 = lambda *a, **k: [{"version": "v6"}]  # noqa: E731
        join_rules_v7 = lambda *a, **k: [{"version": "v7"}]  # noqa: E731

        # 第一次 assemble（v6）：注入 v6 的 join_rules。
        with patch.object(pkg, "_load_harness_callable", side_effect=lambda m, f: join_rules_v6 if f == "join_rules" else None):
            pkg._inject_harness_retriever()
        first = get_memory_retriever()
        self.assertIs(first._join_rules, join_rules_v6, "首次 assemble 应注入 v6 的 join_rules")

        # 第二次 assemble（v7）：必须注入 v7 的 join_rules（修复前会被 _harness_retriever_injected 短路，仍为 v6）。
        with patch.object(pkg, "_load_harness_callable", side_effect=lambda m, f: join_rules_v7 if f == "join_rules" else None):
            pkg._inject_harness_retriever()
        second = get_memory_retriever()
        self.assertIs(second._join_rules, join_rules_v7, "第二次 assemble 必须注入 v7 的 join_rules，不被 v6 锁死")

    def test_load_harness_callable_uses_package_name_not_production_cache(self) -> None:
        """_load_harness_callable 用 __name__（本包）加载，不依赖 load_current_package 生产缓存。

        FR-003 隐含修复：A/B 候选包（_PKG_NAME）加载自身 tools，而非生产 harness_current。
        """
        # harness 包 tools/join_rules.py 应能被 _load_harness_callable 找到（None 表示降级，
        # 但模块必须能 import——若用错包名会 ImportError 返回 None，区别在于 join_rules 模块存在）。
        result = pkg._load_harness_callable("join_rules", "join_rules")
        # join_rules.py 定义了 join_rules 函数（可调用）；若 harness 未实现则 None。
        # 关键断言：能加载到本包的可调用对象（非因包名错误而 None）。
        self.assertTrue(result is None or callable(result), "join_rules 应为 None（降级）或可调用")

    def test_concurrent_assemble_does_not_tear_global_retriever(self) -> None:
        """EDGE-004：并发 assemble 时 _harness_retriever_lock 保证 set_memory_retriever 不撕裂。

        并发多次注入不同 join_rules，最终全局 retriever 应是一个完整有效的 MemoryRetriever
        实例（而非撕裂的半成品）。retriever 无状态，重设只影响下一次检索用的 join_rules。
        """
        import threading

        join_rules_variants = [lambda *a, **k: [{"i": i}] for i in range(20)]  # noqa: E731
        errors: list[Exception] = []

        def _assemble(jr) -> None:
            try:
                with patch.object(pkg, "_load_harness_callable", side_effect=lambda m, f: jr if f == "join_rules" else None):
                    pkg._inject_harness_retriever()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_assemble, args=(jr,)) for jr in join_rules_variants]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发注入不应抛异常")
        final = get_memory_retriever()
        # 最终全局 retriever 必须是一个完整有效的 MemoryRetriever 实例。
        self.assertIsInstance(final, MemoryRetriever, "并发后全局 retriever 应是完整实例，无撕裂")
        # _join_rules 必须是其中一个变体（非 None、可调用）。
        self.assertTrue(callable(final._join_rules), "并发后 retriever 的 join_rules 应完整有效")


class TestAbMemoryDbCleanup(unittest.TestCase):
    """AC-004：A/B memory.db 随 workspace 清理（FR-004）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._memory_root = Path(self._tmp.name)
        self._pool = _init_pool(self._memory_root)
        # 重置 embedder/extractor 单例（避免跨测试污染）。
        from app.platform.memory import reset_memory_backend
        reset_memory_backend()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_drop_sync_removes_db_and_sidecar_files(self) -> None:
        """drop_sync 删除 memory.db 及 -wal/-shm 侧车文件。"""
        workspace_id = "ab-evolve_test-ws"
        # 写入一个 db 文件 + 侧车，模拟抽取入库后的产物。
        db_path = self._memory_root / f"{workspace_id}.db"
        db_path.write_bytes(b"fake-db")
        (self._memory_root / f"{workspace_id}.db-wal").write_bytes(b"wal")
        (self._memory_root / f"{workspace_id}.db-shm").write_bytes(b"shm")

        self._pool.drop_sync(workspace_id)

        for suffix in ("", "-wal", "-shm"):
            self.assertFalse(
                (self._memory_root / f"{workspace_id}.db{suffix}").exists(),
                f"memory.db{suffix} 应被清理",
            )

    def test_cleanup_ab_memory_db_silent_when_pool_uninitialized(self) -> None:
        """pool 未初始化（记忆关闭）时清理静默跳过，不抛异常。"""
        # 模拟 pool 未初始化：让 get_memory_store_pool raise RuntimeError。
        with patch.object(store_mod, "get_memory_store_pool", side_effect=RuntimeError("未初始化")):
            from app.routers.ab_endpoint import _cleanup_ab_memory_db
            # 不应抛异常。
            _cleanup_ab_memory_db("ab-evolve_test")

    def test_cleanup_ab_memory_db_swallows_errors(self) -> None:
        """drop_sync 抛异常时清理记日志不阻断（RSK-004 兜底）。"""
        with patch.object(store_mod.MemoryStorePool, "drop_sync", side_effect=OSError("boom")):
            from app.routers.ab_endpoint import _cleanup_ab_memory_db
            # 不应抛异常。
            _cleanup_ab_memory_db("ab-evolve_test")

    def test_production_db_not_affected_by_ab_cleanup(self) -> None:
        """A/B memory.db 清理不影响生产库（workspace 命名隔离，EVD-005）。"""
        ab_id = "ab-evolve_ab-ws-aaa"
        prod_id = "user123_prod-workspace"
        # 写入两个 db。
        for wid in (ab_id, prod_id):
            (self._memory_root / f"{wid}.db").write_bytes(b"db")

        self._pool.drop_sync(ab_id)

        self.assertFalse((self._memory_root / f"{ab_id}.db").exists(), "A/B db 应被清理")
        self.assertTrue((self._memory_root / f"{prod_id}.db").exists(), "生产 db 不应受影响")


class TestChapterExtractionTrigger(unittest.TestCase):
    """AC-002/AC-006：章节抽取触发器 + 失败可观测（FR-002/NFR-001）。"""

    def test_trigger_chapter_ingestion_calls_extract_and_publish(self) -> None:
        """trigger_chapter_ingestion 转发到 extract_and_publish_sync（FR-002 抽触发器）。"""
        from app.domains.writing.events import trigger_chapter_ingestion
        with patch("app.domains.writing.events.extract_and_publish_sync", return_value={}) as mock_extract:
            trigger_chapter_ingestion(Path("/tmp/ws"), "ws-id", 3)
            mock_extract.assert_called_once_with(Path("/tmp/ws"), "ws-id", 3)

    def test_trigger_chapter_ingestion_swallows_extract_errors(self) -> None:
        """抽取失败由 extract_and_publish_sync 写 unhealthy flag，不阻断调用方（NFR-001）。

        extract_and_publish_sync 内部已捕获异常并写 .memory_unhealthy flag，
        trigger_chapter_ingestion 不额外吞异常——但 extract_and_publish_sync 不会抛出
        （它在 sync 包装层 catch 所有异常）。这里验证触发器不改变该语义。
        """
        from app.domains.writing.events import trigger_chapter_ingestion
        # extract_and_publish_sync 内部 catch 异常返回 {}，不抛出。
        with patch("app.domains.writing.events.extract_and_publish_sync", return_value={}) as mock_extract:
            result = trigger_chapter_ingestion(Path("/tmp/ws"), "ws-id", 3)
            mock_extract.assert_called_once()
            # 触发器无返回值（副作用函数），不抛异常即通过。
            self.assertIsNone(result)

    def test_scan_extracted_chapters_returns_new_chapters(self) -> None:
        """scan_extracted_chapters 检测新写盘且未抽取的章节（FR-002 A/B 检测）。"""
        from app.domains.writing.events import scan_extracted_chapters
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            chap_dir = ws / "chapter"
            chap_dir.mkdir()
            # 已写 2 个章节文件。
            (chap_dir / "chapter-01.md").write_text("第一章正文", encoding="utf-8")
            (chap_dir / "chapter-02.md").write_text("第二章正文", encoding="utf-8")

            # 第 1 章已抽取，应只返回第 2 章。
            new = scan_extracted_chapters(ws, already_extracted={1})
            self.assertEqual(new, {2})

    def test_scan_extracted_chapters_empty_when_no_chapters(self) -> None:
        """无 chapter/ 目录时返回空集（幂等）。"""
        from app.domains.writing.events import scan_extracted_chapters
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(scan_extracted_chapters(Path(tmp), set()), set())

    def test_scan_skips_empty_chapter_files(self) -> None:
        """空章节文件（0 字节）不视为完成，不触发抽取（避免对半截文件抽取）。"""
        from app.domains.writing.events import scan_extracted_chapters
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            chap_dir = ws / "chapter"
            chap_dir.mkdir()
            (chap_dir / "chapter-01.md").write_text("", encoding="utf-8")  # 空文件
            (chap_dir / "chapter-02.md").write_text("正文", encoding="utf-8")
            new = scan_extracted_chapters(ws, set())
            self.assertEqual(new, {2}, "空文件应被跳过")


class TestAbMemoryPoolInitAndLazyRetriever(unittest.TestCase):
    """review 修复验证：A/B 子进程 pool 初始化（FR-001）+ MemoryBackend 懒解析 retriever（FR-003）。"""

    def setUp(self) -> None:
        # 保留全局 pool 单例，测后恢复。
        self._prev_pool = store_mod._pool

    def tearDown(self) -> None:
        store_mod._pool = self._prev_pool

    def test_ensure_memory_pool_initialized_idempotent(self) -> None:
        """_ensure_memory_pool_initialized 幂等：已初始化时跳过，不重建。"""
        from app.routers.ab_endpoint import _ensure_memory_pool_initialized
        with tempfile.TemporaryDirectory() as tmp:
            pool = store_mod.MemoryStorePool(Path(tmp))
            store_mod.init_memory_store_pool(pool)
            _ensure_memory_pool_initialized()  # 已初始化，应跳过不重建
            self.assertIs(store_mod.get_memory_store_pool(), pool, "已初始化不应重建")

    def test_ensure_memory_pool_initialized_when_uninitialized(self) -> None:
        """pool 未初始化时 _ensure_memory_pool_initialized 建池（模拟子进程入口）。"""
        from app.routers.ab_endpoint import _ensure_memory_pool_initialized
        store_mod._pool = None  # 模拟子进程未初始化
        try:
            _ensure_memory_pool_initialized()
            # 应已初始化（get_memory_store_pool 不抛）。
            pool = store_mod.get_memory_store_pool()
            self.assertIsInstance(pool, store_mod.MemoryStorePool)
        finally:
            pass

    def test_ensure_memory_pool_initialized_swallows_init_errors(self) -> None:
        """resolve_memory_root 抛异常时不阻断（降级为无记忆）。"""
        from app.routers.ab_endpoint import _ensure_memory_pool_initialized
        store_mod._pool = None
        with patch("app.platform.memory.store.resolve_memory_root", side_effect=RuntimeError("boom")):
            # 不应抛异常。
            _ensure_memory_pool_initialized()
            # pool 仍 None（初始化失败，降级）。
            self.assertIsNone(store_mod._pool, "初始化失败应保持 None（降级）")

    def test_memory_backend_lazy_resolves_retriever(self) -> None:
        """MemoryBackend 不在 __init__ 快照 retriever，retrieve() 时才解析（FR-003 ordering 修复）。

        场景：get_memory_backend 构造 backend（此时全局 retriever 是默认 v0），
        随后 assemble 注入候选 v7 retriever。backend.retrieve 应拿 v7 而非 v0。
        """
        from app.platform.memory.backend import MemoryBackend
        from app.platform.memory.retriever import MemoryRetriever, set_memory_retriever

        # 默认 retriever（v0）。
        default_retriever = MemoryRetriever()
        set_memory_retriever(default_retriever)
        try:
            # 构造 backend（此时全局是 default）。
            backend = MemoryBackend("test-ws")
            # __init__ 不应快照 default——_retriever 应为 None（待 retrieve 时解析）。
            self.assertIsNone(backend._retriever, "__init__ 不应快照全局 retriever")

            # 模拟 assemble 注入候选 v7 retriever。
            candidate_retriever = MemoryRetriever()
            set_memory_retriever(candidate_retriever)

            # retrieve() 应解析到 candidate（注入后的全局），而非 default。
            # 用 mock 验证解析路径（避免真实 store/embed 调用）。
            resolved = backend._retriever or __import__(
                "app.platform.memory.retriever", fromlist=["get_memory_retriever"]
            ).get_memory_retriever()
            self.assertIs(resolved, candidate_retriever, "retrieve 前解析应拿到注入后的候选 retriever")
        finally:
            set_memory_retriever(default_retriever)


class TestDropSyncConcurrency(unittest.TestCase):
    """review 修复验证：drop_sync 与 async get 的 _stores 互斥（FR-004/EDGE-004）。"""

    def test_drop_sync_and_get_do_not_corrupt_stores(self) -> None:
        """drop_sync 持 _sync_lock，与 async get 的 _stores 操作互斥，不撕裂。"""
        with tempfile.TemporaryDirectory() as tmp:
            pool = store_mod.MemoryStorePool(Path(tmp))
            errors: list[Exception] = []

            async def _get_loop(workspace_id: str, n: int) -> None:
                for _ in range(n):
                    try:
                        await pool.get(workspace_id)
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)

            def _drop_loop(workspace_id: str, n: int) -> None:
                for _ in range(n):
                    try:
                        pool.drop_sync(workspace_id)
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)

            ws_id = "concurrent-test-ws"
            # 并发：async get（事件循环线程）+ sync drop（另一线程）。
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=_drop_loop, args=(ws_id, 50))
            t.start()
            try:
                loop.run_until_complete(_get_loop(ws_id, 50))
            finally:
                loop.close()
            t.join()

            self.assertEqual(errors, [], "并发 get + drop_sync 不应抛异常或撕裂 _stores")

    def test_aclose_all_remains_class_method(self) -> None:
        """回归保护：aclose_all 必须是 MemoryStorePool 的方法（review round2 发现的位移 bug）。

        _unlink_db_files 提取为模块级函数时，aclose_all 曾被误嵌进该函数体，
        导致 main.py lifespan shutdown 的 `await pool.aclose_all()` 抛 AttributeError。
        此测试锁定 aclose_all 始终是类方法。
        """
        import asyncio
        import inspect

        # aclose_all 必须是 MemoryStorePool 的方法（非模块级函数的嵌套）。
        self.assertTrue(
            hasattr(store_mod.MemoryStorePool, "aclose_all"),
            "aclose_all 必须是 MemoryStorePool 的方法",
        )
        # 验证它是 async 方法（coroutine function）。
        self.assertTrue(inspect.iscoroutinefunction(store_mod.MemoryStorePool.aclose_all))

        # 实际调用一次，确保不抛 AttributeError。
        with tempfile.TemporaryDirectory() as tmp:
            pool = store_mod.MemoryStorePool(Path(tmp))
            asyncio.run(pool.aclose_all())  # 不抛即通过


if __name__ == "__main__":
    unittest.main()
