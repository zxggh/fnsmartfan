#!/bin/bash
# SmartFan - 飞牛/FnOS/NAS 一键 Docker 部署脚本
# 用法:  su - root 后  bash deploy-on-fnnas.sh
#
# 与 DEPLOY.md 中的「🥇 方式 1-A」完全等价:
#   - 自动检查运行环境 (root / docker)
#   - 清旧容器 (同名)
#   - 拉取 zxggh/fnsmartfan:latest (amd64 / arm64 双架构自动匹配)
#   - 带全部正确参数 (root user / privileged / 端口 / 卷 / TZ / 健康检查 / 日志限制)
#   - 循环等待 healthy 后打印中文成功提示和访问 URL

set +e

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 0. 颜色 & 工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()   { echo -e "${CYAN}[INFO]${NC}  $*"; }
warn()   { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()    { echo -e "${RED}[ERR ]${NC}  $*"; }
ok()     { echo -e "${GREEN}[ OK ]${NC}  $*"; }
die()    { err "$*"; exit 1; }

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. 环境检查 (必须 root + docker 存在)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " SmartFan 智能风扇控制器 — NAS 一键 Docker 部署"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$(id -u)" -ne 0 ]; then
  die "必须以 root 身份运行! 请先执行:  su - root"
fi
ok "运行身份: root (uid=0)"

if ! command -v docker >/dev/null 2>&1; then
  die "未检测到 docker 命令! 请先在 NAS / 系统中安装并启用 Docker 服务。"
fi
ok "Docker CLI 存在: $(docker --version 2>/dev/null | head -1)"

# 检查 docker daemon 是否在跑
if ! docker info >/dev/null 2>&1; then
  die "Docker daemon 未启动或无权限连接! 请检查 Docker 服务状态 (systemctl start docker)。"
fi
ok "Docker daemon 连接正常"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. 清理旧容器 (相同名字)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
info "清理同名旧容器 ..."
OLD_IDS="$(docker ps -aq --filter name=^/smartfan$ 2>/dev/null)"
if [ -n "$OLD_IDS" ]; then
  docker rm -f smartfan >/dev/null 2>&1 && ok "已删除旧容器 smartfan ($(echo "$OLD_IDS" | wc -l) 个)"
  # 再清一次 name 冲突的僵尸容器
  docker container prune -f >/dev/null 2>&1 || true
else
  ok "没有同名旧容器, 跳过清理"
fi

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. 拉取最新镜像
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMG="zxggh/fnsmartfan:latest"
info "拉取最新镜像 $IMG ... (首次/大版本更新可能需要 1~3 分钟)"
if ! docker pull "$IMG"; then
  err "镜像拉取失败! "
  warn "如果是 Docker Hub 访问慢/超时: "
  warn "  ① 飞牛 NAS → 容器管理 → 设置 → 开启镜像加速器 (阿里云/网易等)"
  warn "  ② 改用 DEPLOY.md 的「🥈 本地构建镜像」方式"
  die "镜像拉取失败, 请检查网络后重试。"
fi
ok "镜像就绪: $(docker inspect "$IMG" --format '{{.Id}}' | cut -c1-16) (大小: $(docker inspect "$IMG" --format '{{.Size}}' | awk '{printf "%.1f MB", $1/1024/1024}'))"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. 启动容器 (全部正确参数, 绝不漏填)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
info "启动容器 smartfan ..."
CID=$(docker run -d \
  --name smartfan \
  --user root \
  --privileged \
  --restart always \
  -p 8780:8780 \
  -v smartfan-data:/data \
  -v /dev:/dev:rw \
  -e TZ=Asia/Shanghai \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=5 \
  --health-cmd "curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1 || exit 1" \
  --health-interval 20s \
  --health-timeout 5s \
  --health-start-period 90s \
  --health-retries 5 \
  "$IMG" 2>/tmp/smartfan-run.err)
RC=$?

if [ $RC -ne 0 ] || [ -z "$CID" ]; then
  err "容器启动失败 (退出码 $RC)!"
  [ -s /tmp/smartfan-run.err ] && { echo "--- docker run 错误详情 ---"; cat /tmp/smartfan-run.err; }
  # 常见错误: 端口占用
  if grep -qi "port is already allocated" /tmp/smartfan-run.err 2>/dev/null; then
    warn "→ 端口 8780 被占用! 解决办法:"
    warn "    ① 停掉占用 8780 的其他容器/程序:  netstat -tlnp | grep 8780"
    warn "    ② 或者把本地端口改为 8781:  把上面命令的 -p 改为 -p 8781:8780"
  fi
  # 常见错误: 名字冲突 (已经清过, 但 daemon 有僵尸的提示)
  if grep -qi "name .* already in use" /tmp/smartfan-run.err 2>/dev/null; then
    warn "→ 容器名字冲突 (daemon 残留僵尸)! 执行下面后重试:"
    warn "    docker rm -f smartfan 2>/dev/null; docker container prune -f; systemctl restart docker"
  fi
  die "无法启动容器, 请根据上面的详情处理后重新运行本脚本。"
fi
ok "容器已创建: ${CID:0:12}"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. 探测 NAS 本机 IP, 打印访问地址
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOST_IP=""
# 飞牛/FnOS 常用网卡
for IF in eth0 ens18 enp1s0 enp3s0 bond0 lan0 wan0; do
  IP=$(ip -4 addr show "$IF" 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -1)
  [ -n "$IP" ] && { HOST_IP="$IP"; break; }
done
[ -z "$HOST_IP" ] && HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$HOST_IP" ] && HOST_IP="<请在 NAS 管理页面查看>"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. 轮询 healthy (最多 2 分钟)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo ""
info "服务正在启动 (首次启动需安装依赖, 最多等待约 2 分钟) ..."
info "  访问地址:  http://$HOST_IP:8780"
echo ""

HEALTHY=0
for i in $(seq 1 24); do
  sleep 5
  S=$(docker inspect smartfan --format '{{.State.Status}}' 2>/dev/null)
  H=$(docker inspect smartfan --format '{{.State.Health.Status}}' 2>/dev/null)
  [ -z "$H" ] && H="-"

  case "$H" in
    healthy)
      echo -e "  $(date '+%H:%M:%S')  状态=${GREEN}$S${NC}  健康=${GREEN}$H${NC}"
      HEALTHY=1
      break
      ;;
    unhealthy)
      echo -e "  $(date '+%H:%M:%S')  状态=${RED}$S${NC}  健康=${RED}$H${NC}"
      ;;
    starting|"-")
      echo -e "  $(date '+%H:%M:%S')  状态=${CYAN}$S${NC}  健康=${CYAN}$H${NC}"
      ;;
    *)
      echo -e "  $(date '+%H:%M:%S')  状态=$S  健康=$H"
      ;;
  esac

  # 如果容器 dead / exited, 提前退出
  if [ "$S" = "exited" ] || [ "$S" = "dead" ]; then
    err "容器异常退出 (Status=$S)! 最后 40 行日志:"
    docker logs --tail 40 smartfan 2>&1
    die "请根据日志排错。数据卷 smartfan-data 不会被删除, 可以放心反复重试。"
  fi
done

echo ""
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. 结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if [ "$HEALTHY" -eq 1 ]; then
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  ok "✅✅✅ SmartFan 部署成功!"
  info "  Web 控制台:  ${GREEN}http://$HOST_IP:8780${NC}"
  info "  容器 ID:    ${CID:0:12}"
  info "  数据卷:     smartfan-data"
  info "  查最新日志:  docker logs --tail 50 smartfan"
  info "  实时跟日志:  docker logs -f smartfan"
  info "  备份数据:    见 DEPLOY.md → 日常维护"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
  warn "⏳ 健康检查仍在进行中 (首次构建 venv + pip 补装依赖需要较长时间) ..."
  info "  请手动访问:  http://$HOST_IP:8780  (等 1~2 分钟刷新几次试试)"
  info "  或者查看实时日志定位:  docker logs -f smartfan"
  echo ""
  warn "如果页面长期打不开, 把 docker logs --tail 80 smartfan 的输出贴到 issue 里。"
fi

exit 0
