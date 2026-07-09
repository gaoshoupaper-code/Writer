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
# DEPLOY_HOST：用 ~/.ssh/config 里的 Host 别名 "writer"（deploy@111.228.4.165:22222）。
#   不要用 root@siyen.site —— 服务器 22 端口已封，只能走 22222 的 deploy 账号。
# REMOTE_DIR：宿主真实路径（nginx 把它以 :ro 挂成容器内 /var/www/releases）。
#   publish 上传到宿主路径，nginx 容器读同一份文件 → /download/ 即可访问。
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
  生成密钥：cd desktop && npx tauri signer generate -w ~/.tauri/siyen.key --force --ci -p \"\"
  然后设置：export TAURI_SIGNING_PRIVATE_KEY=\$(cat ~/.tauri/siyen.key)
  密钥私钥个人保管，绝不上 git（D17）。"
fi

# ── 2. 本地构建 ──────────────────────────────────────────────
info "开始 tauri build（生成签名安装包）..."
cd "$(dirname "$0")/../desktop"
npm run tauri build 2>&1 | tail -5

# ── 3. 读版本号 + 定位产物 ───────────────────────────────────
VERSION=$(grep -oP '"version":\s*"\K[^"]+' src-tauri/tauri.conf.json | head -1)
info "当前版本：v${VERSION}"

# Tauri 2 Windows 产物路径
MSI_DIR="src-tauri/target/release/bundle/msi"
NSIS_DIR="src-tauri/target/release/bundle/nsis"

# ── 3.1 定位 + 签名 + 重命名（MSI + NSIS 双格式）─────────────
# 发布两种格式：
#   .msi  → 企业批量部署
#   .exe  → 普通用户下载（体积更小，SmartScreen 信誉更易积累）
# latest.json 的 updater url 指向 .nsis exe（Tauri 2 updater 默认格式）。
# 统一约定名（download.astro 必须用同一组名字）：
#   siyen-<version>-windows.msi
#   siyen-<version>-windows-setup.exe
TMP_DIR=$(mktemp -d)
UPLOAD_FILES=()

# 处理单个产物：定位 → 显式签名兜底 → 复制为约定名
# 用法：process_bundle <bundle_dir> <ext> <canonical_name> <sig_var_name>
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

# NSIS .exe（自动更新 + 推荐下载）
EXE_CANONICAL="siyen-${VERSION}-windows-setup.exe"
process_bundle "${NSIS_DIR}" exe "${EXE_CANONICAL}" && NSIS_OK=1 || NSIS_OK=0

# MSI（备选下载）
MSI_CANONICAL="siyen-${VERSION}-windows.msi"
process_bundle "${MSI_DIR}" msi "${MSI_CANONICAL}" && MSI_OK=1 || MSI_OK=0

# 两种都没发布 = 失败
[ ${NSIS_OK} -eq 0 ] && [ ${MSI_OK} -eq 0 ] && error "MSI 和 NSIS 产物均未找到"

# ── 4. 生成 latest.json ──────────────────────────────────────
# updater url 优先用 NSIS exe（Tauri 2 updater 原生格式），
# 若只有 MSI 则回退到 MSI。
LATEST_JSON="${TMP_DIR}/latest.json"
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_BINARY="${NSIS_DIR}/$(ls "${NSIS_DIR}" | grep -E '\.exe$' | head -1)"
else
  UPDATER_BINARY="${MSI_DIR}/$(ls "${MSI_DIR}" | grep -E '\.msi$' | head -1)"
fi
SIG_CONTENT=$(cat "${UPDATER_BINARY}.sig")
UPDATER_NAME=$(basename "${UPDATER_BINARY}")
# latest.json 里的 url 用约定名（和 download.astro 一致）
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_URL_NAME="${EXE_CANONICAL}"
else
  UPDATER_URL_NAME="${MSI_CANONICAL}"
fi

# latest.json 格式（Tauri 2 updater 规范）
cat > "${LATEST_JSON}" <<EOF
{
  "version": "${VERSION}",
  "notes": "思衍 v${VERSION}",
  "pub_date": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "platforms": {
    "windows-x86_64": {
      "signature": "${SIG_CONTENT}",
      "url": "${SERVER_URL}/releases/${UPDATER_URL_NAME}"
    }
  }
}
EOF
info "生成 latest.json："
cat "${LATEST_JSON}"

# ── 5. 上传到服务器 ──────────────────────────────────────────
info "上传到 ${DEPLOY_HOST}:${REMOTE_DIR}/ ..."
ssh "${DEPLOY_HOST}" "mkdir -p ${REMOTE_DIR}" || error "SSH 连接失败，检查 DEPLOY_HOST 和密钥配置"
scp "${UPLOAD_FILES[@]}" "${LATEST_JSON}" "${DEPLOY_HOST}:${REMOTE_DIR}/"

# ── 6. 清理旧版安装包（保留最近 3 版）──────────────────────────
# 每次发版后自动清理，避免安装包无限堆积。
# 规则：按版本号降序，保留最新的 KEEP_VERSIONS 个版本的安装包，更早的删除。
#   - 只删本端产物（前缀 siyen-，精确匹配 siyen-*-windows，不误删进化端 siyen-evolution-*）
#   - latest.json 永不删（updater endpoint）
#   - 用 sort -V 做语义版本排序（0.10.0 正确排在 0.9.0 之后）
# KEEP_VERSIONS 可通过环境变量覆盖（如 KEEP_VERSIONS=5 ./publish.sh）。
KEEP_VERSIONS="${KEEP_VERSIONS:-3}"
info "清理旧版安装包（保留最近 ${KEEP_VERSIONS} 版）..."
# 提取所有版本号（从文件名 siyen-<version>-windows.* 解析 <version>），
# 去重 + 版本降序，跳过最新 KEEP_VERSIONS 个，剩余即待删版本。
ssh "${DEPLOY_HOST}" bash -s "${REMOTE_DIR}" "${KEEP_VERSIONS}" <<'CLEANUP' || warn "清理旧包失败（不影响本次发布）"
set -euo pipefail
DIR="$1"
KEEP="$2"
# 待删版本 = 所有版本降序后、跳过最新 KEEP 个的剩余
DELETE_VERSIONS=$(ls "${DIR}"/siyen-*-windows.msi "${DIR}"/siyen-*-windows-setup.exe 2>/dev/null \
  | grep -v 'siyen-evolution-' \
  | sed -E 's|.*/siyen-([0-9]+\.[0-9]+\.[0-9]+)-windows.*|\1|' \
  | sort -Vru \
  | tail -n +"$((${KEEP} + 1))")
if [ -z "${DELETE_VERSIONS}" ]; then
  echo "[INFO] 无旧版需清理"
  exit 0
fi
for v in ${DELETE_VERSIONS}; do
  for f in "${DIR}/siyen-${v}-windows.msi" "${DIR}/siyen-${v}-windows-setup.exe"; do
    if [ -f "${f}" ]; then
      rm -f "${f}"
      echo "[INFO] 已删除旧包：$(basename "${f}")"
    fi
  done
done
CLEANUP

info "发布完成！"
info "  下载页：${SERVER_URL}/download"
[ ${MSI_OK}   -eq 1 ] && info "  MSI 安装包：${SERVER_URL}/releases/${MSI_CANONICAL}"
[ ${NSIS_OK}  -eq 1 ] && info "  EXE 安装包：${SERVER_URL}/releases/${EXE_CANONICAL}"
info "  updater endpoint：${SERVER_URL}/releases/latest.json"
info "  版本：v${VERSION}"

# 清理临时文件
rm -rf "${TMP_DIR}"
