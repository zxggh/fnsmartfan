#!/bin/bash
# ============================================================
# SmartFan 智能风扇控制器 —— 一键卸载脚本
#
# 作用：
#   1. 停止正在运行的 SmartFan 服务
#   2. 移除 crontab 开机自启项
#   3. 提供删除部署目录的命令（让你手动确认后执行，防止误删）
#
# 用法：
#   在任务计划里执行一次：
#     chmod +x "/你的部署目录/uninstall-smartfan.sh"
#     "/你的部署目录/uninstall-smartfan.sh"
# ============================================================

set -e

cd "$(dirname "$0")"
DEPLOY_DIR="$(pwd)"

echo ""
echo "============================================================"
echo "  SmartFan 智能风扇控制器 —— 卸载工具"
echo "============================================================"
echo ""
echo "部署目录: $DEPLOY_DIR"
echo ""

# 1. 停服务（调用同目录下的管理脚本）
if [ -f "manage-smartfan.sh" ]; then
    echo "[1/3] 停止服务..."
    chmod +x manage-smartfan.sh
    ./manage-smartfan.sh stop || true
else
    echo "[1/3] 停止服务（未找到 manage-smartfan.sh，直接 kill）..."
    pkill -f "run-smartfan.sh" 2>/dev/null || true
    pkill -f "start.sh" 2>/dev/null || true
    pkill -f "uvicorn" 2>/dev/null || true
    sleep 1
fi
echo "✅ 服务已停止"

# 2. 清理 crontab 自启
echo ""
echo "[2/3] 移除开机自启..."
if [ -f "manage-smartfan.sh" ]; then
    ./manage-smartfan.sh disable-autostart || true
else
    ( crontab -l 2>/dev/null | grep -v "smartfan\|run-smartfan\|fan-controller" ) | crontab - || true
fi
echo "✅ 自启项已移除"

# 3. 给出删除目录的建议（为了安全，这里只打印命令，不自动删）
echo ""
echo "[3/3] 删除部署文件（需要你手动确认）"
echo ""
echo "⚠️  所有程序、配置、日志都在下面这个目录中："
echo ""
echo "    $DEPLOY_DIR"
echo ""
echo "如果你确认彻底不再使用 SmartFan 服务，可以去飞牛文件管理器"
echo "手动删除整个目录，或者在【任务计划】中执行一次下面这行命令："
echo ""
echo "    rm -rf \"$DEPLOY_DIR\""
echo ""
echo "（↑ 命令不可撤销，请三思）"
echo ""
echo "============================================================"
echo "  ✅ 卸载完成"
echo "============================================================"
echo ""
