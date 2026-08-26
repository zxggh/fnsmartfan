#!/bin/bash
# ============================================================
#  SmartFan 智能风扇控制器 · 一键部署脚本 (万能启动参数版)
#
#  用途: 不会写 docker 命令也能一键部署 (拉镜像+清旧容器+启动+健康验证)
#        完美解决飞牛 NAS GUI "启动失败误判 / 参数漏填" 问题
#
#  用法:
#   1) 把本脚本 quick-start.sh 上传到 NAS 任意可读写目录
#   2) SSH 登录 NAS (或 webSSH) → 切换 root:  su - root
#   3) 进入脚本目录:  cd /你放脚本的路径  (例如 cd /vol1/1000/个人/smartfan)
#   4) 执行:  bash quick-start.sh
#   5) 等 1~3 分钟看结果, 最后会给你浏览器访问地址
#
#  镜像: zxggh/fnsmartfan:latest (Docker Hub 公共, 匿名拉取, 无需 PAT)
#  备用: ghcr.io/zxggh/fnsmartfan:latest (自动 Fallback)
# ============================================================

set +e

# ────────────────── 颜色/辅助 ──────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass() { echo -e "\n  ${GREEN}✅ ${NC}$1"; }
fail() { echo -e "\n  ${RED}❌ ${NC}$1"; }
warn() { echo -e "\n  ${YELLOW}⚠  ${NC}$1"; }
info() { echo -e "\n  ${CYAN}ℹ  ${NC}$1"; }

# ────────────────── 脚本开始 ──────────────────
echo ""
echo "============================================================"
echo " 🌬️  SmartFan · 一键部署 v2.2 (万能启动参数版) "
echo "============================================================"
echo ""

# ═══════════════════════════════════════════════
# 0. 必须 root 执行
# ═══════════════════════════════════════════════
if [ "$(id -u)" -ne 0 ]; then
  fail "请先切到 root 用户再执行!"
  echo "  运行:  su - root    (然后输入 root 密码重新进本目录)"
  echo "  或者:  sudo bash $0"
  exit 1
fi
info "当前是 root, OK"

# ═══════════════════════════════════════════════
# 1. 检查 docker 是否可用
# ═══════════════════════════════════════════════
if ! command -v docker >/dev/null 2>&1; then
  fail "当前系统没装 docker? 找不到 docker 命令!"
  echo "  请先在 NAS 应用商店安装并启用 Docker/容器套件"
  exit 2
fi
DOCKER_VER=$(docker --version 2>/dev/null | head -n 1)
info "Docker 版本: $DOCKER_VER"

# ═══════════════════════════════════════════════
# 2. 端口 8780 检查 (冲突提示)
# ═══════════════════════════════════════════════
PORT=8780
if ss -lntp 2>/dev/null | grep -q ":$PORT " || netstat -lntp 2>/dev/null | grep -q ":$PORT "; then
  warn "端口 $PORT 已被占用! 请先停止占用 8780 的容器/程序后再运行."
  echo "  查看占用:  ss -lntp | grep :$PORT   或  netstat -lntp | grep :$PORT"
  echo "  (如果你想换端口, 修改本脚本顶部 PORT=8780 即可)"
  exit 3
fi
info "端口 $PORT 空闲, OK"

# ═══════════════════════════════════════════════
# 3. 拉取镜像 (Docker Hub 主 + GHCR 备 Fallback)
# ═══════════════════════════════════════════════
MAIN_IMAGE="zxggh/fnsmartfan:latest"
BACKUP_IMAGE="ghcr.io/zxggh/fnsmartfan:latest"
TARGET_IMAGE=""

echo ""
echo "──────────────────────────────────────────────"
echo " [1/3] 拉取镜像 $MAIN_IMAGE (Docker Hub 公共仓库, 匿名)"
echo "──────────────────────────────────────────────"
echo "(超时? 会自动尝试备用 GHCR 仓库, 或 离线 tar 导入)"
sleep 1

PULL_OK=0
get_id() { docker images --format "{{.ID}}" "$1" 2>/dev/null | head -n 1; }

# 先试 Docker Hub
BEFORE=$(get_id "$MAIN_IMAGE")
echo "  docker pull $MAIN_IMAGE ..."
PULL_LOG=$(docker pull "$MAIN_IMAGE" 2>&1 || true)
echo "$PULL_LOG" | tail -8
AFTER=$(get_id "$MAIN_IMAGE")
if [ -n "$AFTER" ] && ([ -z "$BEFORE" ] || [ "$BEFORE" != "$AFTER" ] || true); then
  TARGET_IMAGE="$MAIN_IMAGE"; PULL_OK=1
fi

# Docker Hub 不行, 试 GHCR
if [ "$PULL_OK" -ne 1 ]; then
  warn "Docker Hub 拉取超时/失败, 自动尝试备用仓库 $BACKUP_IMAGE ..."
  BEFORE=$(get_id "$BACKUP_IMAGE")
  PULL_LOG=$(docker pull "$BACKUP_IMAGE" 2>&1 || true)
  echo "$PULL_LOG" | tail -8
  AFTER=$(get_id "$BACKUP_IMAGE")
  if [ -n "$AFTER" ] && ([ -z "$BEFORE" ] || [ "$BEFORE" != "$AFTER" ] || true); then
    info "备用仓库成功! docker tag $BACKUP_IMAGE → $MAIN_IMAGE"
    docker tag "$BACKUP_IMAGE" "$MAIN_IMAGE" 2>/dev/null || true
    TARGET_IMAGE="$MAIN_IMAGE"; PULL_OK=1
  fi
fi

# 最后兜底: 本地有没有任何 fnsmartfan 标签?
if [ "$PULL_OK" -ne 1 ]; then
  ANY=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep -i "fnsmartfan" | head -n 1)
  if [ -n "$ANY" ]; then
    info "发现本地镜像 $ANY, tag 成 $MAIN_IMAGE 兜底"
    docker tag "$ANY" "$MAIN_IMAGE" 2>/dev/null || true
    TARGET_IMAGE="$MAIN_IMAGE"; PULL_OK=1
  fi
fi

if [ "$PULL_OK" -ne 1 ]; then
  fail "所有镜像拉取方式都失败!"
  echo ""
  echo "  请用以下任一办法解决:"
  echo "  办法 A·离线: Windows 电脑执行 -> 上传 tar -> NAS 容器 → 镜像 → 导入:"
  echo "      docker pull $MAIN_IMAGE"
  echo "      docker save $MAIN_IMAGE -o fnsmartfan-latest.tar"
  echo "  办法 B·加速源: NAS Docker 设置里添加国内加速源 (阿里云/中科大/163) 后重试"
  echo "  办法 C·飞牛 GUI: 直接镜像仓库搜 zxggh/fnsmartfan → 拉取 → 用脚本重跑"
  exit 4
fi
pass "已就绪镜像: $TARGET_IMAGE  (IMAGE_ID=$(get_id "$TARGET_IMAGE"))"

# ═══════════════════════════════════════════════
# 4. 清旧容器 + 启动 (三保险万能参数)
#     保险 A: --privileged 特权模式 + -v /dev:/dev-host-dev:ro 宿主机整个 /dev 只读进容器
#             -> 这是兜底中的兜底! 只要 USB 插了, 控制器不管被分到 ttyACM* 几号,
#                容器里 /dev-host-dev/ 下 100% 能看到设备节点, 完全不依赖 --device
#     保险 B: 动态扫描宿主机当前真实存在的 ttyACM(0-9) + ttyUSB(0-9),
#             只 --device 映射真有的节点, 不存在的完全跳过, 再也不会因设备不存在启动失败!
#     保险 C: 就算启动时控制器没插 (保险 B 一个都没映射到),
#             保险 A 依然能兜底 -> 之后任何时候插上控制器, 拔插任何 USB 口,
#             容器都能通过 /dev-host-dev/ 立即识别, 不用重建容器!
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [2/3] 清旧容器 + 启动 smartfan (三保险·万能参数)"
echo "──────────────────────────────────────────────"

# 清旧
info "清理同名旧容器 smartfan ..."
docker rm -f smartfan >/dev/null 2>&1 || true
pass "清场完毕"

# ── 保险 B: 动态扫描宿主机真实存在的串口设备 (0~9 全覆盖) ──
DEVICE_ARGS=()
DETECTED=0
info "扫描宿主机 ttyACM0~9 + ttyUSB0~9 真实存在的设备节点 ..."
for i in $(seq 0 9); do
  if [ -e "/dev/ttyACM$i" ]; then
    DEVICE_ARGS+=("--device")
    DEVICE_ARGS+=("/dev/ttyACM$i:/dev/ttyACM$i")
    DETECTED=$((DETECTED+1))
    echo "    · 发现设备: /dev/ttyACM$i  → 已映射"
  fi
done
for i in $(seq 0 9); do
  if [ -e "/dev/ttyUSB$i" ]; then
    DEVICE_ARGS+=("--device")
    DEVICE_ARGS+=("/dev/ttyUSB$i:/dev/ttyUSB$i")
    DETECTED=$((DETECTED+1))
    echo "    · 发现设备: /dev/ttyUSB$i  → 已映射"
  fi
done

if [ "$DETECTED" -gt 0 ]; then
  pass "保险 B: 动态映射 $DETECTED 个真实串口设备到容器内 /dev/ 下"
else
  warn "保险 B: 当前没发现任何 ttyACM/ttyUSB 设备 (控制器还没插?)"
  info "  没关系! 保险 A 兜底: 下面启动完容器后, 任何时候插上控制器都能立即识别, 不用重建容器!"
fi

# 万能启动 (保险 A: --privileged + -v /dev:/dev-host-dev:ro)
info "docker run 启动容器 (保险 A 特权模式 + /dev-host-dev 兜底 + 保险 B 动态设备映射) ..."
RC=$(docker run -d \
  --name smartfan \
  --user root \
  --privileged \
  --restart always \
  -p "$PORT:$PORT" \
  "${DEVICE_ARGS[@]}" \
  -v smartfan-data:/data \
  -v /dev:/dev-host-dev:ro \
  -e TZ=Asia/Shanghai \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=5 \
  --health-cmd 'curl -s -m 3 http://127.0.0.1:8780/api/info' \
  --health-interval 30s \
  --health-timeout 5s \
  --health-retries 3 \
  --health-start-period 30s \
  "$TARGET_IMAGE" 2>&1)

RUN_RC=$?
if [ "$RUN_RC" -eq 0 ] && [ -n "$RC" ]; then
  pass "容器启动成功! CONTAINER_ID=$(echo "$RC" | head -c 12)"
else
  fail "容器启动失败!"
  echo "  docker run 返回值: $RUN_RC"
  echo "  错误输出: $RC"
  echo ""
  echo "  常见原因: 端口 $PORT 被占用 / docker 服务异常 / 设备文件权限不足"
  exit 5
fi

# ═══════════════════════════════════════════════
# 5. 健康等待 + 验证
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [3/3] 等待健康检查通过 (最多 60 秒)"
echo "──────────────────────────────────────────────"

READY=0
for i in $(seq 1 20); do
  sleep 3
  ST=$(docker inspect -f '{{.State.Status}}' smartfan 2>/dev/null || echo "unknown")
  HC=$(docker inspect -f '{{.State.Health.Status}}' smartfan 2>/dev/null || echo "starting")
  echo -n "  等待 (第 $i/20 次) → 状态=$ST / 健康=$HC    " $'\r'
  if [ "$HC" = "healthy" ]; then
    READY=1; echo ""; break
  fi
  if [ "$ST" = "exited" ]; then
    echo ""
    fail "容器异常退出! 退出码=$(docker inspect -f '{{.State.ExitCode}}' smartfan 2>/dev/null)"
    echo ""
    echo "  最近 50 行日志:"
    docker logs --tail 50 smartfan 2>&1
    exit 6
  fi
done

echo ""
if [ "$READY" -eq 1 ]; then
  pass "容器健康检查通过! (healthy)"
else
  warn "健康检查还没到 healthy, 但容器正在运行, 等 30 秒后刷新页面即可使用"
fi

# 基本 API 验证
info "验证 API 接口 /api/info ..."
API_OUT=$(curl -s -m 5 "http://127.0.0.1:$PORT/api/info" 2>/dev/null || echo "{}")
HAS_AC=$(echo "$API_OUT" | grep -c "auto_cmd_enabled" || true)
if [ "$HAS_AC" -gt 0 ]; then
  pass "API 验证通过: /api/info 含 auto_cmd_enabled (确认是 v2.2+ 镜像)"
else
  warn "API 暂时无法访问 (首次启动 pip 补装依赖中), 请 1 分钟后再刷新"
fi

# ═══════════════════════════════════════════════
# 最终提示
# ═══════════════════════════════════════════════
NAS_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "NAS_IP")

echo ""
echo ""
echo "============================================================"
echo " 🎉  SmartFan 一键部署完成!"
echo "============================================================"
echo ""
echo "  🌐 浏览器立即访问:"
echo "       http://$NAS_IP:$PORT"
echo ""
echo "  📋 部署后 4 项快速验证 (打开 Web UI 后看):"
echo "     ① HDD 温度卡: 首次启动需等待 10~30 秒采集 (已预装 smartmontools)"
echo "     ② 原始命令卡顶部: 有绿色『⏸ 已启用』按钮 (v2 自动命令开关)"
echo "     ③ 控制器未插会显示『未连接』(正常, 插上 USB 自动重连)"
echo "     ④ 手动发 F1CPD=50 → 风扇1 进度条更新 (后续版本优化即时刷新)"
echo ""
echo "  🛠️  日常管理命令 (root 下执行):"
echo "       重启:  docker restart smartfan"
echo "       看日志: docker logs --tail 50 -f smartfan"
echo "       备份配置: docker cp smartfan:/app/config.yaml ./smartfan-config-$(date +%Y%m%d).yaml"
echo "       升级版本: 重新执行一次 bash quick-start.sh (自动拉新镜像)"
echo "============================================================"
