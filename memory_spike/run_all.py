"""一键跑全部 4 个 spike，汇总 verdict。

用法（服务器上）：
  cd ~/Writer/memory_spike
  export SPIKE_LLM_API_KEY=...      # 复用生产 OPENAI_API_KEY
  export SPIKE_LLM_BASE_URL=...     # 复用生产 OPENAI_BASE_URL
  export SPIKE_LLM_MODEL=...        # 复用生产 WRITER_MODEL
  python run_all.py

每个 spike 独立子图（group_id），互不污染。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path


SPIKES = [
    ("spike1_chinese", "中文抽取验证"),
    ("spike2_calendar", "虚构历法验证"),
    ("spike3_alias", "别名消歧验证"),
    ("spike4_cost", "成本 dry-run"),
]


def main() -> None:
    # 检查环境变量
    if not os.environ.get("SPIKE_LLM_API_KEY"):
        print("❌ SPIKE_LLM_API_KEY 未设置")
        print("   export SPIKE_LLM_API_KEY=<生产 OPENAI_API_KEY>")
        sys.exit(1)

    print(f"模型：{os.environ.get('SPIKE_LLM_MODEL', 'gpt-4o-mini')}")
    print(f"Base URL：{os.environ.get('SPIKE_LLM_BASE_URL', '默认 OpenAI')}")
    print()

    results: list[tuple[str, str, int]] = []
    for script, label in SPIKES:
        print("█" * 60)
        print(f"▶ 运行 {label}（{script}）")
        print("█" * 60)
        ret = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "spikes" / f"{script}.py")],
            cwd=str(Path(__file__).parent),
        )
        results.append((script, label, ret.returncode))
        print()

    # 汇总
    print("█" * 60)
    print("汇总")
    print("█" * 60)
    for script, label, code in results:
        status = "✅ 成功" if code == 0 else f"❌ 失败(exit {code})"
        print(f"  {label:<16} {status}")
    print()
    print("各 spike 的 VERDICT 段落见上方输出。")


if __name__ == "__main__":
    main()
