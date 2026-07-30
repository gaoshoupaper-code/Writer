"""受控执行历史 Trace ArtifactRevision 恢复。"""

from __future__ import annotations

import argparse
import json

import app.core.db as db
from app.dossier.recovery import recover_trace_artifacts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover deterministic artifact heads from one trace's governed payloads"
    )
    parser.add_argument("trace_id")
    parser.add_argument("--expected-head-count", type=int)
    args = parser.parse_args()
    db.init_db()
    result = recover_trace_artifacts(
        args.trace_id,
        expected_head_count=args.expected_head_count,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
