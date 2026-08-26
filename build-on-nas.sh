#!/bin/bash
# ============================================================
# SmartFan 智能风扇控制器 —— 在 NAS 上本地构建脚本
#
# 作用：
#   绕过 docker-compose build 时的远程镜像拉取（飞牛401问题）
#   使用 --pull=never 强制只用 NAS 本地已有的 python:3.10-slim 镜像
#
# 前置条件（必须先做完）：
#   1. 在飞牛NAS 容器管理界面 → 拉取镜像 → 搜索 python
#      选择官方 python，标签填 3.10-slim → 拉取完成
#   2. smartfan-install.sh 和 Dockerfile 已上传到当前目录
#
# 用法：
#   cd /你的部署目录
#   chmod +x build-on-nas.sh
#   ./build-on-nas.sh
#
# 构建完成后：
#   docker compose -f docker-compose-offline.yml up -d
# ============================================================

set -e

echo ""
echo "========================================"
echo "  SmartFan 风扇控制器 - NAS 本地构建工具"
echo "========================================"
echo ""

# 检查基础镜像是否存在
if ! docker image inspect python:3.10-slim >/dev/null 2>&1; then
    echo "❌ 错误: 本地未找到 python:3.10-slim 镜像"
    echo ""
    echo "请先按以下步骤操作:"
    echo "  1. 打开飞牛NAS管理界面 → 容器 → 镜像 → 拉取镜像"
    echo "  2. 搜索 python，选择官方 library/python"
    echo "  3. 标签填写: 3.10-slim"
    echo "  4. 确认拉取成功后，再运行本脚本"
    echo ""
    exit 1
fi
echo "✅ 检测到本地 python:3.10-slim 镜像"

# 检查必需文件
if [ ! -f "Dockerfile" ]; then
    echo "❌ 错误: 当前目录未找到 Dockerfile，请确认已上传"
    exit 1
fi
if [ ! -f "smartfan-install.sh" ]; then
    echo "❌ 错误: 当前目录未找到 smartfan-install.sh，请确认已上传"
    exit 1
fi
echo "✅ 检测到 Dockerfile 和 smartfan-install.sh"

echo ""
echo "开始构建（--pull=never 绝不拉远程镜像）..."
echo ""

# 核心构建命令：--pull=never 强制只用本地，不触发远程拉取401
docker build \
    --pull=never \
    -f Dockerfile \
    -t smartfan:latest \
    .

echo ""
echo "========================================"
echo "  ✅ 构建成功！"
echo "========================================"
echo ""
echo "下一步：启动容器"
echo ""
echo "  方法A（命令行）: 执行"
echo "      docker compose -f docker-compose-offline.yml up -d"
echo ""
echo "  方法B（NAS界面）: 使用 docker-compose-offline.yml 创建项目"
echo ""
echo "启动完成后访问:  http://你的NAS_IP:8780"
echo ""
