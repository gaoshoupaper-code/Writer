"""强杀进程组隔离安全测试（CON-008, EVD-019, RSK-006）。

验证 _force_terminate 的 POSIX 分支不会误杀父进程或同组任务：
  - 子进程是进程组长（getpgid==pid）→ killpg 整组（安全）。
  - 子进程非组长（getpgid!=pid）→ 只 kill 单 PID，绝不 killpg（避免误杀父进程组）。

EVD-019 根因：spawn 默认继承父进程组，旧代码无条件 killpg(getpgid(pid)) 会命中
父进程组，可能终止整个 executor + 同组并发任务。修复后先校验组长身份再决定信号目标。

跑法（在 executor 目录）：
    .venv/Scripts/python.exe -m pytest tests/test_force_terminate_isolation.py -v
"""

from __future__ import annotations

import signal
import unittest
from unittest.mock import patch

from app.platform.isolation import worker_process


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class ForceTerminateIsolationTest(unittest.TestCase):
    """CON-008/RSK-006：强杀只能作用于目标 worker 及其派生执行。

    直接 patch worker_process.sys.platform 为 linux，让 POSIX 分支在任意主机上
    都能验证（生产服务器是 Linux，killpg 误杀风险只在那里成立）。
    """

    def _run_posix(self, proc, *, getpgid_ret=None, getpgid_side=None):
        # getpgid/killpg/SIGKILL 在 Windows 上不存在（POSIX-only），测试需注入它们。
        # _force_terminate 在 POSIX 分支内 `import signal` 后用 signal.SIGKILL，
        # 故 patch 内建 signal 模块加一个 SIGKILL 属性（create=True 允许新增）。
        with patch.object(worker_process.sys, "platform", "linux"), \
             patch("signal.SIGKILL", signal.SIGTERM, create=True), \
             patch("app.platform.isolation.worker_process.os.getpgid",
                   return_value=getpgid_ret, side_effect=getpgid_side, create=True) as m_getpgid, \
             patch("app.platform.isolation.worker_process.os.killpg", create=True) as m_killpg, \
             patch("app.platform.isolation.worker_process.os.kill") as m_kill:
            worker_process._force_terminate(proc)
            return m_getpgid, m_killpg, m_kill

    def test_killpg_only_when_child_is_group_leader(self) -> None:
        """子进程是组长（getpgid==pid）→ killpg 整组。"""
        proc = _FakeProcess(pid=99999)
        m_getpgid, m_killpg, m_kill = self._run_posix(proc, getpgid_ret=99999)
        m_getpgid.assert_called_once_with(99999)
        m_killpg.assert_called_once()  # killpg 整组
        m_kill.assert_not_called()  # 组长路径不回退单 PID

    def test_single_pid_kill_when_not_group_leader(self) -> None:
        """子进程非组长（getpgid!=pid，setsid 失败/竞态）→ 只 kill 单 PID，绝不 killpg。

        RSK-006 关键安全网：宁可漏杀派生子进程，也绝不误杀父进程组（pgid=1）。
        """
        proc = _FakeProcess(pid=99999)
        m_getpgid, m_killpg, m_kill = self._run_posix(proc, getpgid_ret=1)
        m_getpgid.assert_called_once_with(99999)
        m_killpg.assert_not_called()  # 非组长绝不 killpg
        m_kill.assert_called_once()  # 只 kill 单 PID

    def test_process_lookup_falls_back_to_single_pid(self) -> None:
        """进程已退出（ProcessLookupError）→ 回退单 PID kill（幂等收尾）。"""
        proc = _FakeProcess(pid=99999)
        m_getpgid, m_killpg, m_kill = self._run_posix(proc, getpgid_side=ProcessLookupError)
        m_killpg.assert_not_called()
        m_kill.assert_called_once()

    def test_windows_uses_taskkill_tree(self) -> None:
        """Windows 走 taskkill /T /F 进程树（不涉及进程组，本就安全）。"""
        proc = _FakeProcess(pid=12345)
        with patch.object(worker_process.sys, "platform", "win32"), \
             patch.object(worker_process.os, "system") as m_system:
            worker_process._force_terminate(proc)
            m_system.assert_called_once()
            self.assertIn("taskkill", m_system.call_args.args[0])
            self.assertIn("/T", m_system.call_args.args[0])  # 进程树
            self.assertIn("/F", m_system.call_args.args[0])  # 强制


if __name__ == "__main__":
    unittest.main()
