#!/bin/bash
# ============================================================
# SmartFan 智能风扇控制器 —— 统一管理脚本
#
# 用法（在任务计划里执行，或SSH执行）：
#   ./manage-smartfan.sh start              启动
#   ./manage-smartfan.sh stop               停止
#   ./manage-smartfan.sh restart            重启
#   ./manage-smartfan.sh status             查看运行状态
#   ./manage-smartfan.sh logs               打印最近 50 行日志
#   ./manage-smartfan.sh enable-autostart   开启开机自启
#   ./manage-smartfan.sh disable-autostart  关闭开机自启
#
# 部署目录就是此脚本所在目录
# ============================================================

cd "$(dirname "$0")"
DEPLOY_DIR="$(pwd)"
ACTION="$1"

# 1. 检查部署完整性
check_deploy() {
    if [ ! -f "run-smartfan.sh" ]; then
        echo "❌ 错误：run-smartfan.sh 不存在，请先执行 deploy-native.sh 完成部署"
        exit 1
    fi
}

# 2. 找 PID
get_pid() {
    if [ -f "smartfan.pid" ]; then
        local pid
        pid="$(cat smartfan.pid 2>/dev/null)"
        if [ -n "$pid" ] && ps -p "$pid" >/dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    # 备用：按进程名找
    pgrep -f "run-smartfan.sh" 2>/dev/null | head -n 1
    return $?
}

case "$ACTION" in
# ========== 启动 ==========
start)
    check_deploy
    if [ -n "$(get_pid)" ]; then
        echo "✅ 服务已经在运行了 (PID: $(get_pid))"
        exit 0
    fi
    echo "▶ 启动 SmartFan 服务..."
    # 先放开串口权限
    for port in /dev/ttyACM*; do
        [ -e "$port" ] && chmod 666 "$port" 2>/dev/null || true
    done
    nohup "$DEPLOY_DIR/run-smartfan.sh" &>/dev/null &
    echo $! > "$DEPLOY_DIR/smartfan.pid"
    disown 2>/dev/null || true
    sleep 3
    if [ -n "$(get_pid)" ]; then
        echo "✅ 启动成功 (PID: $(get_pid))"
        echo "   访问地址: http://<NAS_IP>:8780"
    else
        echo "⚠️  启动失败，请运行:  ./manage-smartfan.sh logs  查看原因"
    fi
    ;;

# ========== 停止 ==========
stop)
    check_deploy
    pid="$(get_pid)"
    if [ -z "$pid" ]; then
        echo "ℹ️  服务未运行"
        exit 0
    fi
    echo "■ 停止 SmartFan 服务 (PID: $pid)..."
    kill "$pid" 2>/dev/null
    # 顺带把可能在跑的 start.sh / uvicorn 也停掉
    pkill -f "start.sh" 2>/dev/null || true
    pkill -f "uvicorn" 2>/dev/null || true
    sleep 2
    pid2="$(get_pid)"
    if [ -n "$pid2" ]; then
        echo "  温柔停止失败，强制结束..."
        kill -9 "$pid2" 2>/dev/null || true
    fi
    rm -f "$DEPLOY_DIR/smartfan.pid"
    echo "✅ 已停止"
    ;;

# ========== 重启 ==========
restart)
    echo "↻ 重启 SmartFan 服务..."
    "$0" stop
    sleep 1
    "$0" start
    ;;

# ========== 状态 ==========
status)
    check_deploy
    pid="$(get_pid)"
    echo ""
    echo "部署目录: $DEPLOY_DIR"
    if [ -n "$pid" ]; then
        echo "服务状态: ✅ 运行中 (PID: $pid)"
        # 看端口
        if command -v ss >/dev/null 2>&1; then
            port="$(ss -tlnp 2>/dev/null | grep ':8780' || true)"
            [ -n "$port" ] && echo "端口状态: ✅ 8780 已监听" || echo "端口状态: ⚠️  未检测到 8780 端口"
        elif command -v netstat >/dev/null 2>&1; then
            port="$(netstat -tlnp 2>/dev/null | grep ':8780' || true)"
            [ -n "$port" ] && echo "端口状态: ✅ 8780 已监听" || echo "端口状态: ⚠️  未检测到 8780 端口"
        fi
    else
        echo "服务状态: ❌ 未运行"
    fi
    # 开机自启
    if crontab -l 2>/dev/null | grep -q "smartfan\|run-smartfan"; then
        echo "开机自启: ✅ 已启用"
    else
        echo "开机自启: ⚠️  未启用"
    fi
    echo ""
    ;;

# ========== 日志 ==========
logs)
    if [ -f "log.txt" ]; then
        echo "========== 最近 50 行日志 (log.txt) =========="
        tail -n 50 log.txt
        echo "================================================"
    else
        echo "ℹ️  日志文件 log.txt 不存在（服务可能还没启动过）"
    fi
    ;;

# ========== 开启开机自启 ==========
enable-autostart)
    check_deploy
    CRON_LINE="@reboot sleep 30; cd \"$DEPLOY_DIR\" && ./run-smartfan.sh"
    # 去掉旧行 + 加新行
    ( crontab -l 2>/dev/null | grep -v "smartfan\|run-smartfan\|fan-controller" ; echo "$CRON_LINE" ) | crontab -
    echo "✅ 开机自启已开启（开机 30 秒后自动启动）"
    ;;

# ========== 关闭开机自启 ==========
disable-autostart)
    # 从 crontab 移除所有相关行
    ( crontab -l 2>/dev/null | grep -v "smartfan\|run-smartfan\|fan-controller" ) | crontab -
    echo "✅ 开机自启已关闭"
    ;;

# ========== 帮助 ==========
""|help|-h|--help)
    echo "SmartFan 管理脚本用法："
    echo ""
    echo "  ./manage-smartfan.sh start              启动服务"
    echo "  ./manage-smartfan.sh stop               停止服务"
    echo "  ./manage-smartfan.sh restart            重启服务"
    echo "  ./manage-smartfan.sh status             查看状态（进程+端口+自启）"
    echo "  ./manage-smartfan.sh logs               查看最近 50 行日志"
    echo "  ./manage-smartfan.sh enable-autostart   开启开机自启"
    echo "  ./manage-smartfan.sh disable-autostart  关闭开机自启"
    echo ""
    ;;

*)
    echo "❌ 未知操作: $ACTION"
    echo "运行 ./manage-smartfan.sh help 查看用法"
    exit 1
    ;;
esac
