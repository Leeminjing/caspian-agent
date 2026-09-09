#!/usr/bin/env bash
# install.sh — Caspian 一键安装器（macOS / Linux，兼容 bash 与 zsh）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Leeminjing/caspian-agent/main/install.sh | sh
#   或： 下载 install.sh 后 `sh install.sh`
#
# 作用：检查 git/python3/docker → 创建 ~/.caspian → git clone main → ~/.caspian/app
#       → python3 -m venv ~/.caspian/runtime/.venv → 装依赖 → 写最小 config/.env
#       → 写 ~/.local/bin/caspian shim → 加入 PATH(profile) → 打印 export key 块。
# 说明：脚本来自互联网，执行前请先审阅；更稳妥可先下载到本地校验哈希。

set -e

REPO_URL="https://github.com/Leeminjing/caspian-agent.git"
CASP_HOME="${HOME}/.caspian"
APP="${CASP_HOME}/app"
VENV="${CASP_HOME}/runtime/.venv"
BIN="${CASP_HOME}/bin"
CONFIG_DIR="${CASP_HOME}/config"
ENV_FILE="${CONFIG_DIR}/.env"

log()  { printf '==> %s\n' "$*"; }
fail() { printf '%s\n' "$*" >&2; exit 1; }

# 1) 前置检查
log "Checking prerequisites..."
missing=""
command -v git >/dev/null 2>&1 || missing="$missing git"
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
command -v docker >/dev/null 2>&1 || missing="$missing docker"
if [ -n "$missing" ]; then
  fail "Missing prerequisites:$missing .  Please install them and re-run the installer."
fi

# python3 版本 >= 3.12
PY3_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
if python3 -c 'import sys; raise SystemExit(not (sys.version_info >= (3, 12)))' 2>/dev/null; then
  :
else
  fail "python3 ${PY3_VERSION} 低于 3.12，请安装更高版本（macOS 可用: brew install python@3.12；Linux 用发行版 python3.12+）。"
fi

# 2) 家目录
log "Creating ${CASP_HOME}"
mkdir -p "${CASP_HOME}" "${CONFIG_DIR}" "${BIN}"

# 3) git clone / pull
log "Cloning ${REPO_URL} -> ${APP}"
if [ -d "${APP}/.git" ]; then
  log "    app already exists; pulling latest."
  git -C "${APP}" fetch origin main
  git -C "${APP}" reset --hard origin/main
else
  if [ -e "${APP}" ]; then rm -rf "${APP}"; fi
  git clone "${REPO_URL}" "${APP}"
fi

# 4) venv
log "Creating virtualenv at ${VENV}"
if [ ! -x "${VENV}/bin/python" ]; then
  python3 -m venv "${VENV}"
fi
VENV_PY="${VENV}/bin/python"

# 5) 依赖（可选国内 pip mirror：设 PIP_INDEX_URL）
log "Installing Python dependencies ([runtime,postgres])..."
"${VENV_PY}" -m pip install --upgrade pip
"${VENV_PY}" -m pip install -e "${APP}/backend/packages/harness[runtime,postgres]"

# 6) 最小 config/.env（随机 JWT_SECRET，不覆盖已存在值）
log "Writing minimal config/.env (JWT_SECRET only; API keys via export)"
if [ -f "${ENV_FILE}" ]; then
  grep -v '^[[:space:]]*JWT_SECRET=' "${ENV_FILE}" > "${ENV_FILE}.tmp" || true
  mv "${ENV_FILE}.tmp" "${ENV_FILE}"
fi
JWT="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
if [ -n "${JWT_SECRET}" ]; then JWT="${JWT_SECRET}"; fi
printf 'JWT_SECRET=%s\n' "${JWT}" >> "${ENV_FILE}"

# 7) PATH shim
log "Writing ${BIN}/caspian"
cat > "${BIN}/caspian" <<SHIM
#!/usr/bin/env bash
exec "${VENV_PY}" -m caspian.cli "\$@"
SHIM
chmod +x "${BIN}/caspian"

# 8) 加入 PATH（bash/zsh profile，幂等）
log "Adding ${BIN} to PATH (~/.zshrc / ~/.bashrc)"
for rc in "${HOME}/.zshrc" "${HOME}/.bashrc"; do
  [ -f "${rc}" ] || : > "${rc}"
  line="export PATH=\"${BIN}:\$PATH\""
  grep -qF "${BIN}" "${rc}" 2>/dev/null || printf '%s\n' "${line}" >> "${rc}"
done

# 9) 打印 export key 块
log "Next: add your API keys to your shell profile (then open a NEW terminal and run 'caspian')"
printf 'export OPENAI_API_KEY="<your DeepSeek key>"\n'
printf 'export DASHSCOPE_API_KEY="<your DashScope key>"\n'
printf 'export OPENAI_BASE_URL="https://api.deepseek.com"   # optional\n'
printf 'export OPENAI_MODEL="deepseek-v4-flash-vision-exp"   # optional\n'
printf '\n'

printf '%s\n' "Caspian installed successfully. Run:"
printf '%s\n' "    caspian"
