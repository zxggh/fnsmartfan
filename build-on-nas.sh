#!/bin/bash
# SmartFan - NAS 本地构建 Docker 镜像 (Docker Hub 访问受限场景用)
#
# 用法 (飞牛/FnOS/NAS, root):
#   ① 把本文件 + Dockerfile + smartfan-install.sh 上传到 NAS 同一目录
#   ② su - root
#   ③ cd 到该目录
#   ④ bash build-on-nas.sh
#   ⑤ 构建成功后, 用 DEPLOY.md 的「方式 1-C 一行版 docker run」启动
#      (把镜像名从 zxggh/fnsmartfan:latest 改为 zxggh/fnsmartfan:local 即可)
#
# 本脚本会:
#   - 检查必备文件 (Dockerfile / smartfan-install.sh)
#   - 检查 docker buildx / build 可用性
#   - 构建当前 CPU 架构的镜像 (tag: zxggh/fnsmartfan:local)
#   - 构建完成后输出建议启动命令

set +e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()   { echo -e "${CYAN}[INFO]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()    { echo -e "${RED}[ERR ]${NC}  $*"; }
ok()     { echo -e "${GREEN}[ OK ]${NC}  $*"; }
die()    { err "$*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || die "无法进入脚本目录 $SCRIPT_DIR"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " SmartFan — NAS 本地构建 Docker 镜像"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# 1. 检查必须文件
for F in Dockerfile smartfan-install.sh; do
  if [ ! -f "$F" ]; then
    die "缺少必须文件: $F (请放在同目录 $SCRIPT_DIR 下)"
  fi
done
ok "必须文件齐全: Dockerfile / smartfan-install.sh"

# 2. 检查 docker build 能力
BUILD_CMD=""
if docker buildx version >/dev/null 2>&1; then
  BUILD_CMD="docker buildx build --load"
elif docker build --help >/dev/null 2>&1; then
  BUILD_CMD="docker build"
else
  die "docker build 命令不可用! 请确保已安装完整的 Docker + BuildKit 组件。"
fi
ok "构建命令: $BUILD_CMD"

# 3. 清理旧镜像 tag
TAG_LOCAL="zxggh/fnsmartfan:local"
info "目标镜像 tag: $TAG_LOCAL"
docker rmi -f "$TAG_LOCAL" >/dev/null 2>&1 || true

# 4. 开始构建 (当前 CPU 架构单平台)
ARCH="$(uname -m)"
info "当前平台架构: $ARCH"
info "开始构建 (首次需要从 python:3.10-slim 拉基础镜像, 可能 3~8 分钟) ..."
START=$(date +%s)

$BUILD_CMD \
  --platform "linux/$ARCH" \
  -t "$TAG_LOCAL" \
  -f Dockerfile \
  . 2>&1 | tee /tmp/smartfan-build.log
RC=${PIPESTATUS[0]}

DUR=$(( $(date +%s) - START ))

# 5. 结果
if [ $RC -ne 0 ]; then
  err "构建失败! (退出码 $RC, 用时 ${DUR}s)"
  warn "最后 60 行构建日志:"
  tail -60 /tmp/smartfan-build.log
  echo ""
  die "请根据日志定位问题。常见原因: ① 基础镜像 python:3.10-slim 拉不到 (配加速器); ② smartfan-install.sh 中 BASE64_DATA 校验失败。"
fi

ok "✅ 构建成功! 用时 ${DUR}s"
SIZE=$(docker inspect "$TAG_LOCAL" --format '{{.Size}}' 2>/dev/null | awk '{printf "%.1f MB", $1/1024/1024}')
info "镜像大小: $SIZE"
info "镜像 ID:  $(docker inspect "$TAG_LOCAL" --format '{{.Id}}' | cut -c1-20)"

# 6. 输出启动建议
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ok "下一步: 启动容器 (复制下面命令粘贴即可)"
echo ""
echo "docker rm -f smartfan 2>/dev/null; \\"
echo "docker run -d --name smartfan \\"
echo "  --user root --privileged --restart always \\"
echo "  -p 8780:8780 \\"
echo "  -v smartfan-data:/data \\"
echo "  -v /dev:/dev:rw \\"
echo "  -e TZ=Asia/Shanghai \\"
echo "  --log-driver json-file --log-opt max-size=10m --log-opt max-file=5 \\"
echo "  $TAG_LOCAL"
echo ""
info "启动后等待 30~90 秒, 然后访问 http://<NAS-IP>:8780"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exit 0
