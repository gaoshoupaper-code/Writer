#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# publish.sh —— 桌面端发布脚本（D15/D17/S8）
# ─────────────────────────────────────────────────────────────
# 流程：
#   1. 自动 bump patch 版本号（tauri.conf.json + Cargo.toml 同步 +1）
#   2. 本地 tauri build 生成 .msi + .exe + .sig（签名密钥从 TAURI_SIGNING_PRIVATE_KEY 环境变量读）
#   3. 读 tauri.conf.json 版本号
#   4. 生成 latest.json（含 download URL + signature）
#   5. scp 上传到服务器 /home/deploy/Writer/releases/（nginx /releases/ 托管）
#   6. 自动 commit + push 版本号变更（download.astro + index.astro + tauri.conf + Cargo.toml）
#
# 设 NO_BUMP=1 可跳过自动 bump（重发同版本时用）。
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

# ── 1. 解析签名密钥 ──────────────────────────────────────────
# 优先级：环境变量 TAURI_SIGNING_PRIVATE_KEY > 默认密钥文件 ~/.tauri/siyen.key。
# 这样日常发版直接 ./publish.sh 即可，不必每次 export；
# 需要用别的密钥时仍可 export TAURI_SIGNING_PRIVATE_KEY=... 覆盖。
# 密钥私钥个人保管，绝不上 git（D17）。
KEY_FILE="${HOME}/.tauri/siyen.key"
if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  if [ -f "${KEY_FILE}" ]; then
    TAURI_SIGNING_PRIVATE_KEY=$(cat "${KEY_FILE}")
    export TAURI_SIGNING_PRIVATE_KEY
    info "从 ${KEY_FILE} 自动读取签名密钥"
  else
    error "未找到签名密钥。
  方式一（自动）：生成密钥到默认路径，脚本会自动读取
    cd desktop && npx tauri signer generate -w ~/.tauri/siyen.key --force --ci -p \"\"
    之后直接 ./scripts/publish.sh 即可
  方式二（手动）：export TAURI_SIGNING_PRIVATE_KEY=\$(cat <你的密钥路径>)
  密钥私钥个人保管，绝不上 git（D17）。"
  fi
fi

# ── 2. 切到构建目录 ─────────────────────────────────────────
cd "$(dirname "$0")/../desktop"

# ── 2.1 自动 bump patch 版本号 ─────────────────────────────
# 发版前把 patch 号 +1，同步写回 tauri.conf.json + Cargo.toml，保证两者一致。
# 曾因两处版本号不一致（tauri.conf=0.1.4 / Cargo.toml=0.1.3）导致编译进二进制的
# 版本与打包元数据错位、updater 版本判断混乱。
# 设 NO_BUMP=1 可跳过（重发同版本 / 手动指定版本号时用）。
if [ "${NO_BUMP:-0}" != "1" ]; then
  CUR_VER=$(grep -oP '"version":\s*"\K[^"]+' src-tauri/tauri.conf.json | head -1)
  # patch 号 +1：MAJOR.MINOR.PATCH → MAJOR.MINOR.(PATCH+1)
  NEW_VER=$(echo "${CUR_VER}" | awk -F. '{printf "%s.%s.%d", $1, $2, $3+1}')
  info "版本号自动 bump：v${CUR_VER} → v${NEW_VER}"
  # 同步写回 tauri.conf.json + Cargo.toml
  sed -i -E "s/(\"version\":\s*\")[^\"]*/\1${NEW_VER}/" src-tauri/tauri.conf.json
  sed -i -E "s/^(version\s*=\s*\")[^\"]*/\1${NEW_VER}/" src-tauri/Cargo.toml
else
  warn "NO_BUMP=1，跳过版本号 bump（重发当前版本）"
fi

# ── 2.2 本地构建 ───────────────────────────────────────────
info "开始 tauri build（生成签名安装包）..."

# 清理 bundle 目录的旧产物（.exe/.msi/.sig）。
# 根因修复：旧版产物残留在 bundle 目录里，导致下方 `ls | head -1` 选错文件，
# 把旧版二进制当新版上传（曾导致 latest.json 声明 0.1.2 但实际传了 0.1.1 的 exe）。
# 清理后保证目录里只有本次构建的产物。
BUNDLE_BASE="src-tauri/target/release/bundle"
info "清理旧 bundle 产物（${BUNDLE_BASE}/{nsis,msi}/*.exe *.msi *.sig）..."
rm -f "${BUNDLE_BASE}"/nsis/*.exe "${BUNDLE_BASE}"/nsis/*.sig 2>/dev/null || true
rm -f "${BUNDLE_BASE}"/msi/*.msi "${BUNDLE_BASE}"/msi/*.sig 2>/dev/null || true

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

# 处理单个产物：定位 → 强制重新签名 → 复制为约定名
# 用法：process_bundle <bundle_dir> <exact_filename> <canonical_name>
#
# 根因修复（cbd46dd 未修干净的致命 bug）：
# 旧实现用 `ls | head -1` 选产物，当 bundle 目录残留多个版本（如 0.1.1 + 0.1.2）
# 时，字典序会让 Siyen_0.1.1 排在 Siyen_0.1.2 前面，导致选错旧版二进制，
# 签名签的是旧版，而 latest.json 声明新版本号 → 签名与二进制不匹配，验签失败。
# 现在改用【精确文件名】定位（productName + VERSION + 架构拼出 Tauri 标准产物名），
# 选错不可能；精确名不存在直接报错退出，让问题尽早暴露而非静默选别的文件。
process_bundle() {
  local bdir="$1" exact="$2" canonical="$3"
  local src sigfile
  src="${bdir}/${exact}"
  [ ! -f "${src}" ] && { warn "未找到产物 ${exact}（${bdir}），跳过"; return 1; }
  sigfile="${src}.sig"
  info "Tauri 产物：$(basename "${src}")"

  # 强制重新签名（不依赖 tauri build 自动签名，也不复用残留 .sig）。
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
# productName=Siyen（tauri.conf.json），arch=x64，Windows nsis=setup.exe / msi=en-US.msi
NSIS_PRODUCT_NAME="Siyen_${VERSION}_x64-setup.exe"
MSI_PRODUCT_NAME="Siyen_${VERSION}_x64_en-US.msi"

# NSIS .exe（自动更新 + 推荐下载）
EXE_CANONICAL="siyen-${VERSION}-windows-setup.exe"
process_bundle "${NSIS_DIR}" "${NSIS_PRODUCT_NAME}" "${EXE_CANONICAL}" && NSIS_OK=1 || NSIS_OK=0

# MSI（备选下载）
MSI_CANONICAL="siyen-${VERSION}-windows.msi"
process_bundle "${MSI_DIR}" "${MSI_PRODUCT_NAME}" "${MSI_CANONICAL}" && MSI_OK=1 || MSI_OK=0

# 两种都没发布 = 失败
[ ${NSIS_OK} -eq 0 ] && [ ${MSI_OK} -eq 0 ] && error "MSI 和 NSIS 产物均未找到"

# ── 3.2 生成 changelog（从 git log 自动提取，写进 latest.json 的 notes）─────
# 用户能在更新横条里看到「本次更新了什么」。
# 规则：
#   - 只取本版本相对上个 tag 新增的 commit（无 tag 则取最近 10 条）
#   - 轻量过滤：去 conventional 前缀(feat/fix/chore…)、去 merge commit、去重、去空行
#   - 输出为换行分隔的纯文本字符串（每行一条），写进 notes（Tauri 要求 notes 是 string）
#   - 前端 UpdateBanner 按换行拆分渲染为列表
# 注意：本函数 stdout 只输出 changelog 文本（被 $(...) 捕获写进 notes），日志必须 >&2。
TAG_PREFIX="creator-v"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo ".")
generate_changelog() {
  local last_tag commits filtered
  last_tag=$(git -C "${REPO_ROOT}" tag -l "${TAG_PREFIX}*" --sort=-version:refname 2>/dev/null | head -1)
  if [ -n "${last_tag}" ]; then
    info "changelog 范围：${last_tag}..HEAD" >&2
    commits=$(git -C "${REPO_ROOT}" log --format="%s" "${last_tag}..HEAD" -- desktop/ 2>/dev/null)
  else
    info "无 ${TAG_PREFIX}* tag，changelog 取最近 10 条 commit" >&2
    commits=$(git -C "${REPO_ROOT}" log --format="%s" -10 -- desktop/ 2>/dev/null)
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

# ── 4. 生成 latest.json ──────────────────────────────────────
# updater url 优先用 NSIS exe（Tauri 2 updater 原生格式），
# 若只有 MSI 则回退到 MSI。
LATEST_JSON="${TMP_DIR}/latest.json"
# 定位 updater 用的二进制（读它的 .sig 写进 latest.json）。
# 用精确文件名（与 process_bundle 一致），不再用 ls|head-1，避免选错版本。
if [ ${NSIS_OK} -eq 1 ]; then
  UPDATER_BINARY="${NSIS_DIR}/${NSIS_PRODUCT_NAME}"
  UPDATER_URL_NAME="${EXE_CANONICAL}"
else
  UPDATER_BINARY="${MSI_DIR}/${MSI_PRODUCT_NAME}"
  UPDATER_URL_NAME="${MSI_CANONICAL}"
fi
SIG_CONTENT=$(cat "${UPDATER_BINARY}.sig")

# latest.json 格式（Tauri 2 updater 规范）
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

# ── 7. 打 git tag（支撑下次发版的增量 changelog）──────────────────
# 下次 publish 时 generate_changelog 会 git log <本tag>..HEAD 取增量 commit。
# 不自动 push（避免脚本意外改 remote），由用户手动 push。
NEW_TAG="${TAG_PREFIX}${VERSION}"
if git rev-parse "${NEW_TAG}" >/dev/null 2>&1; then
  warn "tag ${NEW_TAG} 已存在，跳过打 tag"
else
  git tag "${NEW_TAG}" && info "已打 tag：${NEW_TAG}（记得 git push origin ${NEW_TAG}）"
fi

# ── 8. 联动更新官网下载页版本号（自动 commit + push）──────────────
# download.astro / index.astro 的版本号是硬编码的，发版后需同步更新，
# 否则用户从官网下载到的永远是旧版本号指向的包。
# 改完后自动 commit + push，保证服务器 git pull 能拿到新版本号。
# 但【不】自动部署官网容器——部署是生产操作，由用户确认后手动执行，
# 避免脚本意外触发服务器重建（首次 build 约 5-10 分钟，可能影响线上）。
WEBSITE_DIR="$(dirname "$0")/../website"
DOWNLOAD_ASTRO="${WEBSITE_DIR}/src/pages/download.astro"
INDEX_ASTRO="${WEBSITE_DIR}/src/pages/index.astro"
WEBSITE_CHANGED=0

if [ -f "${DOWNLOAD_ASTRO}" ]; then
  # 替换 CREATOR_VERSION = "x.y.z" → 新版本号
  sed -i -E "s/(CREATOR_VERSION[[:space:]]*=[[:space:]]*\")[^\"]*/\1${VERSION}/" "${DOWNLOAD_ASTRO}" \
    && info "已更新 download.astro 创作端版本号 → v${VERSION}" \
    || warn "更新 download.astro 失败（不影响安装包发布）"
  WEBSITE_CHANGED=1
fi
if [ -f "${INDEX_ASTRO}" ]; then
  # 替换首页 hero meta 的 v0.1.1 → 新版本号
  sed -i -E "s/v[0-9]+\.[0-9]+\.[0-9]+(\s*·)/v${VERSION}\1/" "${INDEX_ASTRO}" \
    && info "已更新 index.astro 首页版本号 → v${VERSION}" \
    || warn "更新 index.astro 失败（不影响安装包发布）"
  WEBSITE_CHANGED=1
fi

# 改了文件才 commit + push；没改动（版本号已是最新）则跳过。
if [ ${WEBSITE_CHANGED} -eq 1 ]; then
  # 统一提交：download.astro + index.astro + tauri.conf.json + Cargo.toml（版本号同步）
  # 只 add 这几个版本号相关文件，不误提交其他未暂存改动。
  git -C "$(dirname "$0")/.." add \
    website/src/pages/download.astro \
    website/src/pages/index.astro \
    desktop/src-tauri/tauri.conf.json \
    desktop/src-tauri/Cargo.toml 2>/dev/null || true
  # 检查是否有 staged 改动（版本号没变时 sed 的改动会被 git 识别为无变化）
  if git -C "$(dirname "$0")/.." diff --cached --quiet; then
    info "版本号已是最新，无需 commit"
  else
    git -C "$(dirname "$0")/.." commit -m "chore(desktop): 版本号同步 → v${VERSION}" \
      && info "已自动 commit 版本号更新（download.astro + index.astro + tauri.conf + Cargo.toml）" \
      || warn "自动 commit 失败，请手动 commit"
    # 自动 push（让服务器 git pull 能拉到新版本号）
    if git -C "$(dirname "$0")/.." push origin HEAD 2>/dev/null; then
      info "已自动 push 到 remote"
    else
      warn "自动 push 失败，请手动执行：git push origin HEAD"
    fi
  fi
  info "⚠️  官网版本号已 commit + push，请在服务器部署官网容器使其生效："
  info "    ssh writer → cd /home/deploy/Writer → git pull"
  info "    docker compose build website && docker compose up -d website"
  info "    （必须 build，不能只 restart —— 版本号在构建时烤进静态 HTML）"
fi

# 清理临时文件
rm -rf "${TMP_DIR}"
