#!/bin/bash
# SmartFan 快速启动入口
# 用法 (Linux, root):  bash quick-start.sh
#
# 这只是一个转发入口: 真正部署脚本是 deploy-on-fnnas.sh,
# 把所有部署逻辑 (清容器/拉镜像/正确参数/状态轮询) 都放在那里,
# 方便 curl 一键调用 (见 DEPLOY.md).

set -e
echo "SmartFan 快速启动入口"
echo "→ 正在调用 deploy-on-fnnas.sh ..."
echo ""

# 优先用同目录下的 deploy-on-fnnas.sh (用户上传了整套文件),
# 如果不存在就从 GitHub 仓库 raw 地址拉取最新版 (纯 curl 环境).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL="$SCRIPT_DIR/deploy-on-fnnas.sh"

if [ -f "$LOCAL" ]; then
  echo "使用本地脚本: $LOCAL"
  chmod +x "$LOCAL"
  exec bash "$LOCAL" "$@"
else
  echo "本地 deploy-on-fnnas.sh 不存在, 从 GitHub 拉取最新版..."
  exec bash -c "$(curl -fsSL https://raw.githubusercontent.com/zxggh/fnsmartfan/main/deploy-on-fnnas.sh)"
fi
