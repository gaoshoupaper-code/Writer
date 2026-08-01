#!/bin/sh
# ════════════════════════════════════════════════════════════════════════════
# docker-entrypoint.sh —— evolution 容器启动前初始化
# ════════════════════════════════════════════════════════════════════════════
# 三项初始化：
#
# 1. harness 独立 git 仓库初始化（去 DB 重构）
#    1a. 旧版迁移：volume 有 current/ 无 repo/ 时，清旧 bare repo + 归档 current/
#    1b. repo/ 种子初始化：repo/ 不存在时从镜像种子 /app/harness_seed/repo 复制
#    1c. git 仓库初始化：init_work_repo() 确保 repo/ 有 .git、remote 配好、
#        main 已 push 到 bare repo（executor 才能 pull）
#    1d. 版本注册表收敛：删除无 commit 的迁移历史，并把当前 production 绑定到 HEAD
#
# 2. golden 基准层同步（原有逻辑）
#    golden 是镜像只读模板，每次启动从 seed 全量覆盖到 volume。
# ════════════════════════════════════════════════════════════════════════════
set -e

# ── 1. harness 独立仓库初始化 ──
# 1a. 旧版迁移检测：重构前 volume 里是 current/（旧结构）+ 旧 bare repo 历史。
#     重构后改用 repo/（独立 git 仓库）。检测到 current/ 存在 = 旧版升级：
#       - 清掉旧 bare repo（旧 commit 历史已废弃，registry.json 重建为新谱系）
#       - 让 entrypoint 从种子初始化全新的 repo/
#     正常新部署/已迁移环境无 current/，跳过本段。
REPO_DIR="/app/evolution/harnesses/repo"
CURRENT_DIR="/app/evolution/harnesses/current"
BARE_REPO="/app/evolution/harness.git"
SEED_REPO="/app/harness_seed/repo"
if [ -d "$CURRENT_DIR" ] && [ ! -d "$REPO_DIR" ]; then
  echo "[entrypoint] 检测到旧版结构 current/，执行去 DB 重构迁移..."
  # BARE_REPO 是 docker volume 挂载点（harness_git:/app/evolution/harness.git），
  # 挂载点目录本身不能 rm（Device or resource busy → 容器崩溃重启循环）。
  # 清空挂载点内的旧 git 对象（删里面的文件，不删挂载点本身），
  # 让后面的 init_work_repo() 从种子重新 init --bare + push 新 harness 代码。
  # 清不空也无妨：init_work_repo 是幂等的，会修正 HEAD 并按需 push。
  if [ -d "$BARE_REPO" ]; then
    echo "[entrypoint] 清空旧 bare repo 内容（旧 commit 历史废弃）：$BARE_REPO"
    find "$BARE_REPO" -mindepth 1 -delete 2>/dev/null || true
  fi
  echo "[entrypoint] 归档旧 current/ → current.legacy/"
  mv "$CURRENT_DIR" "${CURRENT_DIR}.legacy" 2>/dev/null || true
fi

# 1b. repo/ 种子初始化：repo/ 不存在时从镜像种子复制（首次部署或刚完成旧版迁移）。
#     已存在的 repo/ 不覆盖（保留进化历史）。
if [ ! -d "$REPO_DIR" ]; then
  if [ -d "$SEED_REPO" ]; then
    echo "[entrypoint] repo/ 不存在，从种子初始化：$SEED_REPO → $REPO_DIR"
    mkdir -p /app/evolution/harnesses
    cp -r "$SEED_REPO" "$REPO_DIR"
    echo "[entrypoint] repo/ 种子复制完成"
  else
    echo "[entrypoint] ⚠ repo/ 不存在且无种子（$SEED_REPO），将尝试空目录初始化"
    mkdir -p "$REPO_DIR"
  fi
fi

# 1b-升级. 受控系统中间件升级（C3 / FR-004 / EVD-007 / FR-001）：
#     repo/ 已存在时，把镜像 seed 中新增的承重系统文件（middleware/）合并进来。
#     只合并 seed 有但 repo 没有的文件（新增），不覆盖用户进化改动过的同名文件
#     （冲突时记录到升级日志，不静默覆盖——EDGE-006 / RSK-003）。
#     这样 ArtifactSnapshotMiddleware 等镜像新增的承重中间件能进入活跃 Harness。
#     FR-001 关键：cp 后必须把新增承重文件 commit 进 HEAD tree，否则它们停留在
#     untracked，commit_candidate 的 cat-file 校验必崩（CON-001 单一真相源）。
if [ -d "$SEED_REPO/middleware" ] && [ -d "$REPO_DIR/middleware" ]; then
  echo "[entrypoint] 受控系统中间件升级：检查 seed 新增的承重文件..."
  UPGRADE_LOG="/app/evolution/harnesses/.system_upgrade.log"
  echo "[upgrade $(date -u +%FT%TZ)] 开始系统中间件升级" > "$UPGRADE_LOG" || true
  conflicts=0; added=0
  for seed_file in "$SEED_REPO"/middleware/*.py; do
    [ -f "$seed_file" ] || continue
    fname=$(basename "$seed_file")
    target="$REPO_DIR/middleware/$fname"
    if [ ! -f "$target" ]; then
      # 新增文件：合并进来（镜像带来的承重系统中间件）。
      cp "$seed_file" "$target"
      echo "  + 新增 $fname" >> "$UPGRADE_LOG"
      added=$((added + 1))
    else
      # 已存在：比较内容，不同则记录冲突（不覆盖用户改动）。
      if ! cmp -s "$seed_file" "$target"; then
        echo "  ! 冲突 $fname（用户有改动，保留现状）" >> "$UPGRADE_LOG"
        conflicts=$((conflicts + 1))
      fi
    fi
  done
  echo "[upgrade] 完成: 新增 $added 个, 冲突 $conflicts 个（保留现状）" >> "$UPGRADE_LOG"
  echo "[entrypoint] 系统中间件升级完成: 新增 $added, 冲突 $conflicts（详见 $UPGRADE_LOG）"

  # FR-001：新增的承重中间件文件必须进 HEAD tree，否则 commit_candidate 的
  # required_paths 校验（cat-file HEAD:middleware/xxx.py）必然 rc=128。
  # 只在有新增文件且 repo 已是 git 仓库时提交（首次部署由 init_work_repo 的
  # 首次 commit 统一收编，无需此处重复）。
  if [ "$added" -gt 0 ] && [ -d "$REPO_DIR/.git" ]; then
    echo "[entrypoint] 把 $added 个新增承重中间件 commit 进 HEAD tree..."
    cd "$REPO_DIR" || true
    git add middleware/*.py 2>>"$UPGRADE_LOG" || true
    # 只在有暂存内容时 commit（避免空 commit 报错；-c 提供独立 author 不依赖全局配置）
    if git diff --cached --quiet 2>/dev/null; then
      echo "  （无新增可 commit，已全部在 HEAD）" >> "$UPGRADE_LOG"
    else
      git -c user.name=evolution -c user.email=evolution@local \
        commit -m "承重中间件升级：纳入 $added 个新增系统中间件（FR-001）" \
        >>"$UPGRADE_LOG" 2>&1 || echo "  ⚠ commit 失败（非致命）" >> "$UPGRADE_LOG"
      # 对齐 bare repo（executor pull 的源），fast-forward 优先
      git push origin main >>"$UPGRADE_LOG" 2>&1 \
        || git push --force-with-lease origin main >>"$UPGRADE_LOG" 2>&1 \
        || echo "  ⚠ push 到 bare repo 失败（非致命，init_work_repo 会重试）" >> "$UPGRADE_LOG"
    fi
    cd /app/evolution || true
  fi
fi

# 1c. git 仓库初始化（repo/ 有 .git 则跳过，无则 init + 首次 commit + push）
echo "[entrypoint] 初始化 harness 独立 git 仓库..."
cd /app/evolution && python -c "
from app.core.git_ops import init_work_repo
init_work_repo()
print('[entrypoint] harness 仓库初始化完成')
" || echo "[entrypoint] ⚠ harness 仓库初始化失败（非致命，继续启动）"

# 1d. 显式 commit 绑定上线前的历史版本无法安全执行。保留当前 production 作为
#     新谱系根（工作树 HEAD 就是当前实际包），删除其余无绑定记录，避免前端继续展示。
cd /app/evolution && python -c "
from app.core.git_ops import commit_registry_metadata_and_push, current_commit
from app.versioning.registry_repo import prune_unexecutable_history_and_bind_production
result = prune_unexecutable_history_and_bind_production(current_commit())
if result['changed']:
    commit_registry_metadata_and_push('收敛 Harness production 的不可变 commit 绑定')
print('[entrypoint] harness 版本注册表收敛:', result)
" || echo "[entrypoint] ⚠ harness 版本注册表收敛失败（非致命，继续启动）"

# ── 2. golden 基准层同步 ──
SEED_DIR="/app/evalset_seed/golden"
TARGET_DIR="/app/evolution/data/evalset/golden"

if [ -d "$SEED_DIR" ]; then
  echo "[entrypoint] 同步 golden 种子到 volume：$SEED_DIR → $TARGET_DIR"
  mkdir -p /app/evolution/data/evalset
  rm -rf "$TARGET_DIR"
  cp -r "$SEED_DIR" "$TARGET_DIR"
  echo "[entrypoint] golden 同步完成：$(ls "$TARGET_DIR" | wc -l) 个 case"
else
  echo "[entrypoint] 无 golden 种子目录（$SEED_DIR），跳过同步"
fi

# 交接给 CMD（uvicorn）
exec "$@"
