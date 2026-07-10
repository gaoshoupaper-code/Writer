"""golden revision 锁定机制（数据闭环设计 A4）。

决策 D4：golden 锁 case 列表 + demand 内容。改任一项 = 新 revision。
revision 必须精确反映 golden 目录的内容，不能依赖整个项目 repo 的 commit
（后者包含无关改动）。

方案：对 evalset/golden/ 目录计算内容指纹（SHA-256，递归拼接所有 demand.md
+ reference.md 的内容 hash）。只要 golden 下任何文件内容/增删 case 变化，
指纹就变。比 git tree hash 更简单（不依赖 git 状态），确定性可复现。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.common.evalset import evalset_root, layer_root

logger = logging.getLogger("evolution.dataset.revision")

# 参与指纹计算的文件名（按 case 目录内）
_FINGERPRINT_FILES = ("demand.md", "reference.md")


def compute_golden_revision() -> str:
    """计算当前 golden 目录的内容指纹（SHA-256 前 12 位）。

    递归遍历 evalset/golden/<case_id>/ 下所有 _FINGERPRINT_FILES 文件，
    按路径排序后拼接内容做 SHA-256。只要任何 case 的内容变化/增删 case，指纹就变。

    Returns:
        12 字符 hex 指纹；golden 目录不存在或为空返回 "empty"。
    """
    golden_dir = layer_root("golden")
    if not golden_dir.exists():
        return "empty"

    hasher = hashlib.sha256()
    case_dirs = sorted(
        d for d in golden_dir.iterdir() if d.is_dir() and (d / "demand.md").exists()
    )
    if not case_dirs:
        return "empty"

    for case_dir in case_dirs:
        # case 目录名参与指纹（case_id 增删也算内容变化）
        hasher.update(case_dir.name.encode("utf-8"))
        for fname in _FINGERPRINT_FILES:
            fpath = case_dir / fname
            if fpath.exists():
                content = fpath.read_bytes()
                hasher.update(fname.encode("utf-8"))
                hasher.update(content)

    return hasher.hexdigest()[:12]


def lock_golden_revision() -> str:
    """计算当前 golden 内容指纹并返回（供写入 dataset_meta 锁定）。

    调用时机：golden 内容经 git 变更后重新锁定 revision。
    """
    revision = compute_golden_revision()
    logger.info("golden revision 锁定: %s", revision)
    return revision


def verify_golden_intact(expected_revision: str) -> bool:
    """校验当前 golden 内容是否与锁定的 revision 一致。

    用于 benchmark runner 启动前校验 golden 未被意外篡改。
    """
    current = compute_golden_revision()
    return current == expected_revision


__all__ = [
    "compute_golden_revision",
    "lock_golden_revision",
    "verify_golden_intact",
]
