#!/usr/bin/env python3
"""保守迁移历史 Harness 版本的 commit 绑定（C2 / FR-005 / DEC-006 / EDGE-005）。

扫描现有 registry.json 的 versions，为有唯一可复查证据的旧版本补 commit_hash
显式绑定；无法证明的版本标记 executable=False，保留历史记录但禁止执行。

迁移规则（DEC-006）：
  - 优先用 registry 已有的 source_session → 查 git log 找该 session 产出时的 commit
  - 或用 release_events_v2 的 candidate_id → commit 映射
  - 只有唯一、可复查证据充分时才迁移绑定
  - 无法证明的版本：标记 executable=False + migration_reason，不删除历史

用法：python scripts/migrate-version-bindings.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从 evolution 容器内或仓库根运行。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "evolution") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "evolution"))


def migrate(dry_run: bool = False) -> None:
    from app.core.settings import settings
    from app.core import git_ops

    registry_path = settings.harness_work_dir_path / "registry.json"
    if not registry_path.exists():
        print(f"registry.json 不存在: {registry_path}")
        return

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    versions = data.get("versions", [])
    migrated = 0
    unprovable = 0

    for v in versions:
        # 已有显式绑定的跳过。
        if v.get("commit_hash"):
            print(f"  v{v['version']}: 已有 commit 绑定 {v['commit_hash'][:8]}, 跳过")
            continue

        # 尝试证明：用 source_session 查 git log（该 session 的 commit）。
        source_session = v.get("source_session")
        proven_commit = _try_prove_commit(git_ops, v, source_session)

        if proven_commit:
            v["commit_hash"] = proven_commit
            v["executable"] = True
            v["migration_source"] = "git_log_session_lookup"
            migrated += 1
            print(f"  v{v['version']}: 迁移绑定 → {proven_commit[:8]} (session={source_session})")
        else:
            # 无法证明：标记不可执行，保留历史（DEC-006）。
            v["executable"] = False
            v["migration_reason"] = "no_provable_commit_binding"
            unprovable += 1
            print(f"  v{v['version']}: 无可证明绑定，标记不可执行")

    if dry_run:
        print(f"\n[DRY RUN] 迁移 {migrated} 个版本，标记 {unprovable} 个不可执行（未写入）")
        return

    data["versions"] = versions
    registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n迁移完成: {migrated} 个版本绑定 commit, {unprovable} 个标记不可执行")


def _try_prove_commit(git_ops, version_entry: dict, source_session: str | None) -> str | None:
    """尝试用可复查证据证明版本的 commit 绑定。

    保守策略：只有唯一明确的证据才迁移。多个候选或无候选都返回 None。
    """
    if not source_session:
        return None
    try:
        log = git_ops.log_oneline()
        # 查含 session_id 的 commit（commit message 含 session=xxx）。
        candidates = [
            line.split()[0]
            for line in log
            if source_session in line
        ]
        # 唯一匹配才迁移——多个匹配无法确定（DEC-006 唯一可复查证据）。
        if len(candidates) == 1:
            return candidates[0]
    except Exception:
        pass
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="迁移历史 Harness 版本 commit 绑定")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
