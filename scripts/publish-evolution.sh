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
npm run tauri build 2>&1 | tail -5

# ── 3. 读版本号 + 定位产物 ───────────────────────────────────
VERSION=$(grep -oP '"version":\s*"\K[^"]+' src-tauri/tauri.conf.json | head -1)
info "进化端当前版本：v${VERSION}"

MSI_DIR="src-tauri/target/release/bundle/msi"
NSIS_DIR="src-tauri/target/release/bundle/nsis"

# ── 3.1 定位 + 签名 + 重命名（MSI + NSIS 双格式）─────────────
TMP_DIR=$(mktemp -d)
UPLOAD_FILES=()

# 处理单个产物：定位 → 显式签名兜底 → 复制为约定名
process_bundle() {
  local bdir="$1" ext="$2" canonical="$3"
  local src sigfile
  src=$(ls "${bdir}"/*."${ext}" 2>/dev/null | head -1)
  [ -z "${src}" ] && { warn "未找到 .${ext} 产物（${bdir}），跳过"; return 1; }
  sigfile="${src}.sig"
  info "Tauri 产物：$(basename "${src}")"

  # 显式签名兜底：tauri build 在某些配置下不会自动签名
  if [ ! -f "${sigfile}" ]; then
    info "未检测到自动生成的 .sig，显式签名中..."
    npx tauri signer sign "${src}" >/dev/null || error "签名失败，检查 TAURI_SIGNING_PRIVATE_KEY"
    info "签名完成：${sigfile}"
  fi

  cp "${src}" "${TMP_DIR}/${canonical}"
  UPLOAD_FILES+=("${TMP_DIR}/${canonical}")
  info "发布文件名（约定）：${canonical}"
  return 0
}

# NSIS .exe（自动更新 + 推荐下载），约定名带 -evolution 区分
EXE_CANONICAL="siyen-evolution-${VERSION}-windows-setup.exe"
process_bundle "${NSIS_DIR}" exe "${EXE_CANONICAL}" && NSIS_OK=1 || NSIS_OK=0

# MSI（备选下载）
MSI_CANONICAL="siyen-evolution-${VERSION}-windows.msi"
process_bundle "${MSI_DIR}" msi "${MSI_CANONICAL}" && MSI_OK=1 || MSI_OK=0

# 两种都没发布 = 失败
[ ${NSIS_OK} -eq 0 ] && [ ${MSI_OK} -eq 0 ] && error "MSI 和 NSIS 产物均未找到"

# ── 4. 生成 latest-evo.json ──────────────────────────────────
# 进化端独立 updater endpoint（与写作端 latest.json 分开）
LATEST_JSON="${TMP_DIR}/latest-evo.json"
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_BINARY="${NSIS_DIR}/$(ls "${NSIS_DIR}" | grep -E '\.exe$' | head -1)"
else
  UPDATER_BINARY="${MSI_DIR}/$(ls "${MSI_DIR}" | grep -E '\.msi$' | head -1)"
fi
SIG_CONTENT=$(cat "${UPDATER_BINARY}.sig")
# latest-evo.json 里的 url 用约定名（带 -evolution）
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_URL_NAME="${EXE_CANONICAL}"
else
  UPDATER_URL_NAME="${MSI_CANONICAL}"
fi

# latest.json 格式（Tauri 2 updater 规范）
cat > "${LATEST_JSON}" <<EOF
{
  "version": "${VERSION}",
  "notes": "思衍进化 v${VERSION}",
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

info "进化端发布完成！"
[ ${MSI_OK}  -eq 1 ] && info "  MSI 安装包：${SERVER_URL}/releases/${MSI_CANONICAL}"
[ ${NSIS_OK} -eq 1 ] && info "  EXE 安装包：${SERVER_URL}/releases/${EXE_CANONICAL}"
info "  updater endpoint：${SERVER_URL}/releases/latest-evo.json"
info "  版本：v${VERSION}"

# 清理临时文件
rm -rf "${TMP_DIR}"
