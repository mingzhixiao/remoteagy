#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [ -f ".env" ]; then
  set -a
  source .env
  set +a
fi

PORT="${REMOTEAGY_PORT:-7682}"
LOG_FILE="/var/log/remoteagy.log"
SERVICE_NAME="remoteagy.service"

if [ -x "$APP_DIR/.venv/bin/python" ]; then
  PY_BIN="$APP_DIR/.venv/bin/python"
else
  PY_BIN="$(command -v python3)"
fi

has_systemd_service() {
  command -v systemctl >/dev/null 2>&1 \
    && [ -d /run/systemd/system ] \
    && systemctl list-unit-files "$SERVICE_NAME" 2>/dev/null | grep -q "$SERVICE_NAME"
}

get_port_pid() {
  ss -tlnp "sport = :$PORT" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -n1 | cut -d= -f2 || true
}

cmd_start() {
  if has_systemd_service; then
    systemctl start "$SERVICE_NAME"
    echo "[OK] 已通过 systemd 启动 $SERVICE_NAME"
    return
  fi
  local pid
  pid="$(get_port_pid)"
  if [ -n "$pid" ]; then
    echo "[OK] RemoteAgy 已在运行中 (PID=$pid, 端口=$PORT)"
    return
  fi
  setsid "$PY_BIN" -m uvicorn server:app --host 127.0.0.1 --port "$PORT" </dev/null >"$LOG_FILE" 2>&1 &
  sleep 2
  pid="$(get_port_pid)"
  if [ -n "$pid" ]; then
    echo "[OK] RemoteAgy 启动成功 (PID=$pid, 监听=127.0.0.1:$PORT)"
  else
    echo "[错误] RemoteAgy 启动失败，请查看日志: $LOG_FILE"
    exit 1
  fi
}

cmd_stop() {
  if has_systemd_service; then
    systemctl stop "$SERVICE_NAME"
    echo "[OK] 已停止 $SERVICE_NAME"
    return
  fi
  local pid
  pid="$(get_port_pid)"
  if [ -z "$pid" ]; then
    echo "[OK] RemoteAgy 未在运行"
    return
  fi
  kill "$pid"
  for _ in 1 2 3 4 5; do
    sleep 1
    if [ -z "$(get_port_pid)" ]; then
      echo "[OK] RemoteAgy 已停止 (PID=$pid)"
      return
    fi
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "[OK] RemoteAgy 已强制停止 (PID=$pid)"
}

cmd_restart() {
  if has_systemd_service; then
    systemctl restart "$SERVICE_NAME"
    echo "[OK] 已重启 $SERVICE_NAME"
    return
  fi
  cmd_stop
  cmd_start
}

cmd_status() {
  if has_systemd_service; then
    systemctl status "$SERVICE_NAME" --no-pager
    return
  fi
  local pid
  pid="$(get_port_pid)"
  if [ -n "$pid" ]; then
    echo "状态 : 运行中 (PID=$pid)"
    echo "监听 : 127.0.0.1:$PORT"
    echo "日志 : $LOG_FILE"
  else
    echo "状态 : 未运行"
    return 1
  fi
}

cmd_logs() {
  if has_systemd_service; then
    journalctl -u "$SERVICE_NAME" -n 100 -f
  else
    if [ ! -f "$LOG_FILE" ]; then
      echo "暂无日志文件: $LOG_FILE"
      exit 1
    fi
    tail -n 100 -f "$LOG_FILE"
  fi
}

cmd_update() {
  echo "[1/3] 拉取最新代码..."
  git pull --ff-only
  echo "[2/3] 更新依赖..."
  "$PY_BIN" -m pip install -r requirements.txt -q || true
  echo "[3/3] 重启服务..."
  cmd_restart
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  update)  cmd_update ;;
  *)
    echo "用法: $0 {start|stop|restart|status|logs|update}"
    exit 1
    ;;
esac
