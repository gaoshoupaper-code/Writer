#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# publish-evolution.sh —— 进化端桌面端发布脚本（2026-07-08）
# ─────────────────────────────────────────────────────────────
# 流程（照搬 publish.sh，适配进化端）：
#   1. 本地 tauri build 生成 .msi + .exe + .sig（签名密钥从 TAURI_SIGNING_PRIVATE_KEY 读）
#   2. 读 tauri.conf.json 版本号
#   3. 生成 latest-evo.json（进化端独立 updater endpoint，与写作端 latest.json 分开）
#   4. scp 上传到服务器 /home/deploy/Writer/releases/（nginx /releases/ 托管）
#
# 与 publish.sh 的区别：
#   - 构建目录：evolution/desktop（非 desktop）
#   - 产物约定名：siyen-evolution-<version>-windows-*（带 -evolution 区分）
#   - updater endpoint：latest-evo.json（非 latest.json），两端独立发版互不干扰
#   - 复用同一把签名密钥（公钥已写进两端 tauri.conf.json）
#
# 前置：
#   - 设置环境变量 TAURI_SIGNING_PRIVATE_KEY（与写作端同一把，个人保管不上 git）
#   - SSH 配置好 DEPLOY_HOST（~/.ssh/config 的 Host 别名 "writer"）
#
# 用法：
#   TAURI_SIGNING_PRIVATE_KEY=xxx ./scripts/publish-evolution.sh
#   或先 export 再跑
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# 配置（与 publish.sh 一致，仅构建目录和产物名不同）
DEPLOY_HOST="${DEPLOY_HOST:-writer}"
REMOTE_DIR="${REMOTE_DIR:-/home/deploy/Writer/releases}"
SERVER_URL="${SERVER_URL:-https://siyen.site}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 1. 检查环境变量 ──────────────────────────────────────────
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  error "未设置 TAURI_SIGNING_PRIVATE_KEY 环境变量。
  进化端复用写作端同一把签名密钥。设置：
  export TAURI_SIGNING_PRIVATE_KEY=\$(cat ~/.tauri/siyen.key)
  密钥私钥个人保管，绝不上 git。"
fi

# ── 2. 本地构建 ──────────────────────────────────────────────
info "开始 tauri build（进化端，生成签名安装包）..."
cd "$(dirname "$0")/../evolution/desktop"

# 清理 bundle 目录的旧产物（.exe/.msi/.sig）。
# 根因修复：旧版产物残留导致 `ls | head -1` 选错文件，把旧版当新版上传。
BUNDLE_BASE="src-tauri/target/release/bundle"
info "清理旧 bundle 产物（${BUNDLE_BASE}/{nsis,msi}/*.exe *.msi *.sig）..."
rm -f "${BUNDLE_BASE}"/nsis/*.exe "${BUNDLE_BASE}"/nsis/*.sig 2>/dev/null || true
rm -f "${BUNDLE_BASE}"/msi/*.msi "${BUNDLE_BASE}"/msi/*.sig 2>/dev/null || true

npm run tauri build 2>&1 | tail -5

# ── 3. 读版本号 + 定位产物 ───────────────────────────────────
VERSION=$(grep -oP '"version":\s*"\K[^"]+' src-tauri/tauri.conf.json | head -1)
info "进化端当前版本：v${VERSION}"

MSI_DIR="src-tauri/target/release/bundle/msi"
NSIS_DIR="src-tauri/target/release/bundle/nsis"

# ── 3.1 定位 + 签名 + 重命名（MSI + NSIS 双格式）─────────────
TMP_DIR=$(mktemp -d)
UPLOAD_FILES=()

# 处理单个产物：定位 → 强制重新签名 → 复制为约定名
# 用法：process_bundle <bundle_dir> <exact_filename> <canonical_name>
#
# 根因修复（与 publish.sh 同源 bug）：旧实现用 `ls | head -1` 选产物，bundle 目录
# 残留多版本时按字典序选错旧版，导致签名与二进制不匹配、验签失败。
# 现改用【精确文件名】定位（productName + VERSION + 架构拼出 Tauri 标准产物名），
# 选错不可能；精确名不存在直接跳过（MSI 可选）/报错。
process_bundle() {
  local bdir="$1" exact="$2" canonical="$3"
  local src sigfile
  src="${bdir}/${exact}"
  [ ! -f "${src}" ] && { warn "未找到产物 ${exact}（${bdir}），跳过"; return 1; }
  sigfile="${src}.sig"
  info "Tauri 产物：$(basename "${src}")"

  # 强制重新签名（不复用残留 .sig，避免签名与二进制不匹配导致验签失败）
  info "签名中（强制重新签名）..."
  rm -f "${sigfile}"
  npx tauri signer sign "${src}" >/dev/null || error "签名失败，检查 TAURI_SIGNING_PRIVATE_KEY / PASSWORD"

  # 签名自检：解码 .sig 的 trusted comment 里的 file: 字段，
  # 断言它等于实际签名的文件名。不匹配说明签错了文件，立即终止。
  # 这道关卡确保"签名错配"的 bug 永不再现。
  local sig_base64 sig_decoded sig_file
  sig_base64=$(cat "${sigfile}")
  sig_decoded=$(echo "${sig_base64}" | base64 -d 2>/dev/null || true)
  sig_file=$(echo "${sig_decoded}" | grep -oP 'file:\K.*' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)
  if [ -n "${sig_file}" ] && [ "${sig_file}" != "$(basename "${src}")" ]; then
    error "签名自检失败：.sig 指向 '${sig_file}'，但实际签名的是 '$(basename "${src}")'。产物选择错误，终止发布。"
  fi
  info "签名完成（自检通过）：$(basename "${sigfile}")"

  cp "${src}" "${TMP_DIR}/${canonical}"
  UPLOAD_FILES+=("${TMP_DIR}/${canonical}")
  info "发布文件名（约定）：${canonical}"
  return 0
}

# Tauri 2 标准产物名：{productName}_{version}_{arch}-{target}.{ext}
# productName="Siyen Evolution"（tauri.conf.json，注意含空格）
NSIS_PRODUCT_NAME="Siyen Evolution_${VERSION}_x64-setup.exe"
MSI_PRODUCT_NAME="Siyen Evolution_${VERSION}_x64_en-US.msi"

# NSIS .exe（自动更新 + 推荐下载），约定名带 -evolution 区分
EXE_CANONICAL="siyen-evolution-${VERSION}-windows-setup.exe"
process_bundle "${NSIS_DIR}" "${NSIS_PRODUCT_NAME}" "${EXE_CANONICAL}" && NSIS_OK=1 || NSIS_OK=0

# MSI（备选下载，进化端默认不构建，找不到会 warn 跳过）
MSI_CANONICAL="siyen-evolution-${VERSION}-windows.msi"
process_bundle "${MSI_DIR}" "${MSI_PRODUCT_NAME}" "${MSI_CANONICAL}" && MSI_OK=1 || MSI_OK=0

# 两种都没发布 = 失败
[ ${NSIS_OK} -eq 0 ] && [ ${MSI_OK} -eq 0 ] && error "MSI 和 NSIS 产物均未找到"

# ── 3.2 生成 changelog（从 git log 自动提取，写进 latest-evo.json 的 notes）─────
# 用户能在更新横条里看到「本次更新了什么」。
# 规则：
#   - 只取本版本相对上个 tag 新增的 commit（无 tag 则取最近 10 条）
#   - 轻量过滤：去 conventional 前缀(feat/fix/chore…)、去 merge commit、去重、去空行
#   - 输出为换行分隔的纯文本字符串（每行一条），写进 notes（Tauri 要求 notes 是 string）
#   - 前端 UpdateBanner 按换行拆分渲染为列表
# 注意：本函数 stdout 只输出 changelog 文本（被 $(...) 捕获写进 notes），日志必须 >&2。
TAG_PREFIX="evo-v"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
generate_changelog() {
  local last_tag commits filtered
  last_tag=$(git -C "${REPO_ROOT}" tag -l "${TAG_PREFIX}*" --sort=-version:refname 2>/dev/null | head -1)
  if [ -n "${last_tag}" ]; then
    info "changelog 范围：${last_tag}..HEAD" >&2
    commits=$(git -C "${REPO_ROOT}" log --format="%s" "${last_tag}..HEAD" -- evolution/ 2>/dev/null)
  else
    info "无 ${TAG_PREFIX}* tag，changelog 取最近 10 条 commit" >&2
    commits=$(git -C "${REPO_ROOT}" log --format="%s" -10 -- evolution/ 2>/dev/null)
  fi
  filtered=$(echo "${commits}" \
    | grep -v '^Merge' \
    | sed -E 's/^(feat|fix|chore|refactor|docs|style|test|perf|ci|build|revert)(\([^)]*\))?(!)?:[[:space:]]*//' \
    | grep -v '^[[:space:]]*$' \
    | awk '!seen[$0]++')
  echo "${filtered}"
}
# CHANGELOG_JSON 是换行分隔的文本，用 python 转成合法 JSON 字符串（带引号和 \n 转义），
# 直接放进 heredoc 的 notes 字段（notes 必须是 JSON string 类型）。
CHANGELOG_RAW=$(generate_changelog)
CHANGELOG_JSON=$(echo "${CHANGELOG_RAW}" | python -c '
import sys, json
text = sys.stdin.read().strip()
lines = [l.strip() for l in text.splitlines() if l.strip()]
print(json.dumps("\n".join(lines), ensure_ascii=False))
')
info "changelog：${CHANGELOG_JSON}"

# ── 4. 生成 latest-evo.json ──────────────────────────────────
# 进化端独立 updater endpoint（与写作端 latest.json 分开）
LATEST_JSON="${TMP_DIR}/latest-evo.json"
# 定位 updater 用的二进制（读它的 .sig 写进 latest-evo.json）。
# 用精确文件名（与 process_bundle 一致），不再用 ls|head-1，避免选错版本。
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_BINARY="${NSIS_DIR}/${NSIS_PRODUCT_NAME}"
  UPDATER_URL_NAME="${EXE_CANONICAL}"
else
  UPDATER_BINARY="${MSI_DIR}/${MSI_PRODUCT_NAME}"
  UPDATER_URL_NAME="${MSI_CANONICAL}"
fi
SIG_CONTENT=$(cat "${UPDATER_BINARY}.sig")

# latest-evo.json 格式（Tauri 2 updater 规范）
# notes 是 JSON string 类型（Tauri 强制要求）。
# CHANGELOG_JSON 是 python json.dumps() 输出的合法 JSON 字符串（带引号、\n 转义），直接展开。
cat > "${LATEST_JSON}" <<EOF
{
  "version": "${VERSION}",
  "notes": ${CHANGELOG_JSON},
  "pub_date": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "platforms": {
    "windows-x86_64": {
      "signature": "${SIG_CONTENT}",
      "url": "${SERVER_URL}/releases/${UPDATER_URL_NAME}"
    }
  }
}
EOF
info "生成 latest-evo.json："
cat "${LATEST_JSON}"

# ── 5. 上传到服务器 ──────────────────────────────────────────
info "上传到 ${DEPLOY_HOST}:${REMOTE_DIR}/ ..."
ssh "${DEPLOY_HOST}" "mkdir -p ${REMOTE_DIR}" || error "SSH 连接失败，检查 DEPLOY_HOST 和密钥配置"
scp "${UPLOAD_FILES[@]}" "${LATEST_JSON}" "${DEPLOY_HOST}:${REMOTE_DIR}/"

# ── 6. 清理旧版安装包（保留最近 3 版）──────────────────────────
# 每次发版后自动清理，避免安装包无限堆积。
# 规则：按版本号降序，保留最新的 KEEP_VERSIONS 个版本的安装包，更早的删除。
#   - 只删进化端产物（前缀 siyen-evolution-）
#   - latest-evo.json 永不删（updater endpoint）
#   - 用 sort -V 做语义版本排序
# KEEP_VERSIONS 可通过环境变量覆盖（如 KEEP_VERSIONS=5 ./publish-evolution.sh）。
KEEP_VERSIONS="${KEEP_VERSIONS:-3}"
info "清理旧版安装包（保留最近 ${KEEP_VERSIONS} 版）..."
ssh "${DEPLOY_HOST}" bash -s "${REMOTE_DIR}" "${KEEP_VERSIONS}" <<'CLEANUP' || warn "清理旧包失败（不影响本次发布）"
set -euo pipefail
DIR="$1"
KEEP="$2"
# 从 siyen-evolution-<version>-windows.* 解析版本号，去重 + 降序，跳过最新 KEEP 个
DELETE_VERSIONS=$(ls "${DIR}"/siyen-evolution-*-windows.msi "${DIR}"/siyen-evolution-*-windows-setup.exe 2>/dev/null \
  | sed -E 's|.*/siyen-evolution-([0-9]+\.[0-9]+\.[0-9]+)-windows.*|\1|' \
  | sort -Vru \
  | tail -n +"$((${KEEP} + 1))")
if [ -z "${DELETE_VERSIONS}" ]; then
  echo "[INFO] 无旧版需清理"
  exit 0
fi
for v in ${DELETE_VERSIONS}; do
  for f in "${DIR}/siyen-evolution-${v}-windows.msi" "${DIR}/siyen-evolution-${v}-windows-setup.exe"; do
    if [ -f "${f}" ]; then
      rm -f "${f}"
      echo "[INFO] 已删除旧包：$(basename "${f}")"
    fi
  done
done
CLEANUP

info "进化端发布完成！"
[ ${MSI_OK}  -eq 1 ] && info "  MSI 安装包：${SERVER_URL}/releases/${MSI_CANONICAL}"
[ ${NSIS_OK} -eq 1 ] && info "  EXE 安装包：${SERVER_URL}/releases/${EXE_CANONICAL}"
info "  updater endpoint：${SERVER_URL}/releases/latest-evo.json"
info "  版本：v${VERSION}"

# ── 7. 打 git tag（支撑下次发版的增量 changelog）──────────────────
NEW_TAG="${TAG_PREFIX}${VERSION}"
if git rev-parse "${NEW_TAG}" >/dev/null 2>&1; then
  warn "tag ${NEW_TAG} 已存在，跳过打 tag"
else
  git tag "${NEW_TAG}" && info "已打 tag：${NEW_TAG}（记得 git push origin ${NEW_TAG}）"
fi

# ── 8. 联动更新官网下载页版本号 ──────────────────────────────
# download.astro 的进化端版本号是硬编码的，发版后需同步更新。
WEBSITE_DIR="$(dirname "$0")/../website"
DOWNLOAD_ASTRO="${WEBSITE_DIR}/src/pages/download.astro"
if [ -f "${DOWNLOAD_ASTRO}" ]; then
  sed -i -E "s/(EVOLUTION_VERSION[[:space:]]*=[[:space:]]*\")[^\"]*/\1${VERSION}/" "${DOWNLOAD_ASTRO}" \
    && info "已更新 download.astro 进化端版本号 → v${VERSION}" \
    || warn "更新 download.astro 失败（不影响安装包发布）"
  info "⚠️  官网页面已更新版本号，请手动执行："
  info "    git add website/ && git commit -m 'chore(website): 下载页进化端版本号 → v${VERSION}'"
  info "    然后在服务器重新部署官网容器（docker-compose build website && docker-compose up -d website）"
fi

# 清理临时文件
rm -rf "${TMP_DIR}"
