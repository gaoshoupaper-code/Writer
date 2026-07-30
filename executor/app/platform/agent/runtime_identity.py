"""冻结可复现的 executor、依赖与 Harness 运行身份。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from functools import lru_cache
from importlib.metadata import distributions, version
from pathlib import Path
from typing import Any


_KEY_DISTRIBUTIONS = (
    "deepagents",
    "langchain",
    "langchain-core",
    "langchain-openai",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "openai",
)


def build_runtime_identity(*, harness_root: Path, harness_commit: str | None = None) -> dict[str, Any]:
    """返回 snapshot、probe 和 production activation 共用的精确身份。"""
    identity = dict(_base_runtime_identity())
    identity["harness_commit"] = harness_commit or _git_value(
        harness_root, ["rev-parse", "HEAD"]
    )
    identity["harness_dirty"] = bool(_git_value(
        harness_root, ["status", "--porcelain"]
    ))
    identity["artifact_snapshot_middleware"] = (
        harness_root / "middleware" / "artifact_snapshot.py"
    ).is_file()
    identity["platform_artifact_capture"] = True
    identity["identity_digest"] = _digest(identity)
    return identity


@lru_cache(maxsize=1)
def _base_runtime_identity() -> dict[str, Any]:
    installed = sorted(
        {
            f"{str(item.metadata.get('Name') or '').lower()}=={item.version}"
            for item in distributions()
            if item.metadata.get("Name")
        }
    )
    dependencies = {}
    for name in _KEY_DISTRIBUTIONS:
        try:
            dependencies[name] = version(name)
        except Exception:
            dependencies[name] = None
    executor_root = Path(__file__).resolve().parents[3]
    return {
        "executor_code_digest": _tree_digest(executor_root),
        "executor_image_id": os.getenv("EXECUTOR_IMAGE_ID") or None,
        "dependency_lock_digest": hashlib.sha256(
            "\n".join(installed).encode("utf-8")
        ).hexdigest(),
        "dependencies": dependencies,
    }


def _tree_digest(executor_root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted((executor_root / "app").rglob("*.py"))
    pyproject = executor_root / "pyproject.toml"
    if pyproject.is_file():
        paths.append(pyproject)
    for path in paths:
        digest.update(path.relative_to(executor_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_value(root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["build_runtime_identity"]
