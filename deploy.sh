#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "=== RemoteAgy 一键部署 ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3，请先安装 Python 3.10+"
  exit 1
fi

# 1. 检测 agy CLI
AGY_PATH="$(command -v agy || echo "$HOME/.local/bin/agy")"
if [ ! -x "$AGY_PATH" ]; then
  echo "[警告] 未检测到可执行的 agy ($AGY_PATH)，请确保已安装 Antigravity CLI 并在 .env 中配置 AGY_BIN。"
fi

# 2. 准备虚拟环境与依赖
if [ ! -d ".venv" ]; then
  echo "[1/4] 创建 Python 虚拟环境 (.venv)..."
  python3 -m venv .venv || true
fi

if [ -x ".venv/bin/python" ]; then
  PY_BIN="$APP_DIR/.venv/bin/python"
  "$PY_BIN" -m pip install --upgrade pip -q
  "$PY_BIN" -m pip install -r requirements.txt -q
else
  PY_BIN="$(command -v python3)"
  echo "[1/4] 使用系统 Python 安装依赖..."
  "$PY_BIN" -m pip install -r requirements.txt --break-system-packages -q || true
fi

# 3. 初始化 .env 配置
if [ ! -f ".env" ]; then
  echo "[2/4] 生成默认 .env 配置文件..."
  RAND_PASS=$("$PY_BIN" -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16)))')
  cat > .env <<EOF
REMOTEAGY_USER=agy
REMOTEAGY_PASS=$RAND_PASS
REMOTEAGY_PORT=7682
DEFAULT_CWD=$APP_DIR
PROJECTS_ROOT=$(dirname "$APP_DIR")
AGY_BIN=$AGY_PATH
EOF
  chmod 600 .env
else
  echo "[2/4] 检测到已有 .env，保留现有配置。"
fi

set -a
source .env
set +a
PORT="${REMOTEAGY_PORT:-7682}"

# 4. 注册 systemd 服务（若可用）或后台启动
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ] && [ -d /run/systemd/system ]; then
  echo "[3/4] 配置并启动 systemd 服务 (remoteagy.service)..."
  cat > /etc/systemd/system/remoteagy.service <<EOF
[Unit]
Description=RemoteAgy Web & WeChat ClawBot Service
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PY_BIN -m uvicorn server:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now remoteagy.service
else
  echo "[3/4] 后台启动 uvicorn 服务..."
  setsid "$PY_BIN" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" </dev/null >/var/log/remoteagy.log 2>&1 &
  sleep 2
fi

echo "[4/4] 部署完成！"
echo "  监听地址 : http://127.0.0.1:$PORT"
echo "  登录用户 : ${REMOTEAGY_USER:-agy}"
echo "  登录密码 : ${REMOTEAGY_PASS:-请查看 .env}"
echo "  提示     : 建议通过 Nginx / OpenResty / Caddy 反向代理配置 HTTPS 后再从公网访问。"
