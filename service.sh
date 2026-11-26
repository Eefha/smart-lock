#!/usr/bin/env bash
# ============================================================
# 共享行李锁服务管理脚本（裸 nohup 场景）
#   ./service.sh start|stop|restart|status|logs
#
# 环境变量从同目录 secrets.env 读取（该文件权限必须为 600，且不要上传到代码库）。
# 上传到 Linux 后若报 "bad interpreter" 或行尾异常，先执行：
#   sed -i 's/\r$//' service.sh
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

SECRETS_FILE="$APP_DIR/secrets.env"
LOG_DIR="$APP_DIR/logs"
PID_DIR="$LOG_DIR/pids"
PYTHON_BIN="${PYTHON_BIN:-python3}"

SERVICES=("tcp_server.py" "admin_web.py")

mkdir -p "$LOG_DIR" "$PID_DIR"

# ---------- 加载密钥 ----------
# 缺失或为空都不再阻断启动：config.py 首次启动会自动生成随机密钥并写入 secrets.env
load_secrets() {
    if [ ! -f "$SECRETS_FILE" ]; then
        echo "ℹ️  未找到 $SECRETS_FILE，首次启动将由 config.py 自动生成密钥并写入该文件"
        return 0
    fi
    if [ "$(stat -c '%a' "$SECRETS_FILE" 2>/dev/null || echo 600)" != "600" ]; then
        echo "⚠️  $SECRETS_FILE 权限不是 600，已自动收紧"
        chmod 600 "$SECRETS_FILE"
    fi
    set -a
    # shellcheck disable=SC1090
    source "$SECRETS_FILE" || true
    set +a

    for key in TOKEN_SECRET INTERNAL_API_KEY; do
        if [ -z "${!key:-}" ]; then
            echo "ℹ️  $SECRETS_FILE 中 $key 为空，将由 config.py 自动生成"
        fi
    done
}

# ---------- 进程判定 ----------
pid_of() {
    local name="$1"
    local pidfile="$PID_DIR/${name%.py}.pid"
    if [ -f "$pidfile" ]; then
        local pid
        pid="$(cat "$pidfile")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # 确认该 PID 确实是目标脚本，避免 PID 复用误判
            if grep -q "$name" "/proc/$pid/cmdline" 2>/dev/null; then
                echo "$pid"
                return 0
            fi
        fi
        rm -f "$pidfile"
    fi
    return 1
}

start_one() {
    local name="$1"
    local tag="${name%.py}"
    if pid_of "$name" >/dev/null; then
        echo "▶ $name 已在运行 (PID $(pid_of "$name"))，跳过"
        return 0
    fi
    nohup "$PYTHON_BIN" "$name" >> "$LOG_DIR/$tag.log" 2>&1 &
    echo $! > "$PID_DIR/$tag.pid"
    echo "✅ $name 已启动 (PID $!)，日志 $LOG_DIR/$tag.log"
}

stop_one() {
    local name="$1"
    local pid
    if pid="$(pid_of "$name")"; then
        kill "$pid"
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "⚠️  $name (PID $pid) 未在 5 秒内退出，强制 SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_DIR/${name%.py}.pid"
        echo "⏹ $name 已停止"
    else
        echo "▶ $name 未在运行"
    fi
}

status() {
    local name
    for name in "${SERVICES[@]}"; do
        if pid="$(pid_of "$name")"; then
            echo "✅ $name  运行中  PID $pid"
        else
            echo "⚪ $name  未运行"
        fi
    done
    echo "---- 端口监听 ----"
    (ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | grep -E ':(8001|8002|8003)\b' || echo "（未检测到 8001/8002/8003 监听）"
}

case "${1:-}" in
    start)
        load_secrets
        start_one tcp_server.py
        sleep 2          # tcp_server 内部还要拉起 Flask，错开一点避免同时抢连接池
        start_one admin_web.py
        sleep 2
        status
        ;;
    stop)
        stop_one admin_web.py
        stop_one tcp_server.py
        ;;
    restart)
        "${BASH_SOURCE[0]}" stop || true
        sleep 1
        "${BASH_SOURCE[0]}" start
        ;;
    status)
        status
        ;;
    logs)
        tail -n 60 -f "$LOG_DIR/tcp.log" "$LOG_DIR/admin.log"
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs}" >&2
        exit 2
        ;;
esac
