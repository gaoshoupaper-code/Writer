#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# publish.sh —— 桌面端发布脚本（D15/D17/S8）
# ─────────────────────────────────────────────────────────────
# 流程：
#   1. 本地 tauri build 生成 .msi + .sig（签名密钥从 TAURI_SIGNING_PRIVATE_KEY 环境变量读）
#   2. 读 tauri.conf.json 版本号
#   3. 生成 latest.json（含 download URL + signature）
#   4. scp 上传到服务器 /var/www/releases/（nginx /download/ 托管）
#
# 前置：
#   - 设置环境变量 TAURI_SIGNING_PRIVATE_KEY（D17：个人保管，不上 git）
#   - 设置环境变量 TAURI_SIGNING_PRIVATE_KEY_PASSWORD（如有密码）
#   - 服务器 SSH 配置好（DEPLOY_HOST）
#
# 用法：
#   TAURI_SIGNING_PRIVATE_KEY=xxx ./scripts/publish.sh
#   或先 export 再跑
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# 配置（按需修改）
DEPLOY_HOST="${DEPLOY_HOST:-root@siyen.site}"
REMOTE_DIR="${REMOTE_DIR:-/var/www/releases}"
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
  生成密钥：cd desktop && npx tauri signer generate -w ~/.tauri/writer.key
  然后设置：export TAURI_SIGNING_PRIVATE_KEY=\$(cat ~/.tauri/writer.key)
  密钥私钥个人保管，绝不上 git（D17）。"
fi

# ── 2. 本地构建 ──────────────────────────────────────────────
info "开始 tauri build（生成签名安装包）..."
cd "$(dirname "$0")/../desktop"
npm run tauri build 2>&1 | tail -5

# ── 3. 读版本号 + 定位产物 ───────────────────────────────────
VERSION=$(grep -oP '"version":\s*"\K[^"]+' src-tauri/tauri.conf.json | head -1)
info "当前版本：v${VERSION}"

# Tauri 2 Windows 产物路径：src-tauri/target/release/bundle/msi/*.msi
BUNDLE_DIR="src-tauri/target/release/bundle/msi"
MSI_FILE=$(ls "${BUNDLE_DIR}"/*.msi 2>/dev/null | head -1) || error "未找到 .msi 产物"
SIG_FILE="${MSI_FILE}.sig"

if [ ! -f "${SIG_FILE}" ]; then
  error "未找到签名文件 ${SIG_FILE}。确认 TAURI_SIGNING_PRIVATE_KEY 正确。"
fi

MSI_NAME=$(basename "${MSI_FILE}")
info "产物：${MSI_NAME}"

# ── 4. 生成 latest.json ──────────────────────────────────────
TMP_DIR=$(mktemp -d)
LATEST_JSON="${TMP_DIR}/latest.json"
SIG_CONTENT=$(cat "${SIG_FILE}")

# latest.json 格式（Tauri 2 updater 规范）
cat > "${LATEST_JSON}" <<EOF
{
  "version": "${VERSION}",
  "notes": "Writer v${VERSION}",
  "pub_date": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "platforms": {
    "windows-x86_64": {
      "signature": "${SIG_CONTENT}",
      "url": "${SERVER_URL}/download/${MSI_NAME}"
    }
  }
}
EOF
info "生成 latest.json："
cat "${LATEST_JSON}"

# ── 5. 上传到服务器 ──────────────────────────────────────────
info "上传到 ${DEPLOY_HOST}:${REMOTE_DIR}/ ..."
ssh "${DEPLOY_HOST}" "mkdir -p ${REMOTE_DIR}" || error "SSH 连接失败，检查 DEPLOY_HOST 和密钥配置"
scp "${MSI_FILE}" "${LATEST_JSON}" "${DEPLOY_HOST}:${REMOTE_DIR}/"

info "发布完成！"
info "  下载页：${SERVER_URL}/download/"
info "  updater endpoint：${SERVER_URL}/download/latest.json"
info "  版本：v${VERSION}"

# 清理临时文件
rm -rf "${TMP_DIR}"
