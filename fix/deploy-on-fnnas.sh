#!/bin/bash
# ============================================================
#  SmartFan 智能风扇控制器 — 飞牛 NAS 一键部署脚本
#
#  使用方法 (SSH 登录飞牛 NAS 后执行, 必须 root):
#    chmod +x deploy-on-fnnas.sh
#    ./deploy-on-fnnas.sh
#
#  特性:
#    ✓ 自动清理旧容器/旧镜像缓存
#    ✓ 100% 正确参数: root + privileged + 设备映射 + 卷 + 端口
#      (避免 GUI 创建时漏勾参数导致的各种坑)
#    ✓ 启动后状态轮询打印, 直到 healthy (中文进度提示)
#    ✓ 健康检查阈值已内置: start-period=90s, 不会误判启动失败
#    ✓ 日志大小限制 (10M × 5 文件), 不会撑爆 NAS 硬盘
# ============================================================

set +e   # 容错: 单步失败不中断脚本, 给出中文提示

CONTAINER_NAME="smartfan"
IMAGE="zxggh/fnsmartfan:latest"
DATA_VOLUME="smartfan-data"
PORT="8780"
TZ="Asia/Shanghai"

# ---- 必须 root 执行 ----
if [ "$(id -u)" != "0" ]; then
  echo "❌ 请先执行 su - root 切换到 root 再运行本脚本! (UID=$(id -u))"
  exit 1
fi

echo "=================================================================="
echo "  SmartFan 一键部署 (飞牛 NAS)"
echo "=================================================================="
echo ""

# ---- 1. 清理旧容器 ----
echo "🗑️  [1/5] 清理旧容器 $CONTAINER_NAME ..."
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1
echo "   ✓ 完成"

# ---- 2. 拉取最新镜像 ----
echo "🐳 [2/5] 拉取最新镜像 $IMAGE ..."
docker pull "$IMAGE"
RC=$?
if [ $RC -ne 0 ]; then
  echo "⚠️  镜像拉取失败 (网络/认证问题), 尝试使用本地已有镜像继续启动..."
  docker inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "❌ 本地也不存在 $IMAGE 镜像, 无法继续!"
    exit 2
  }
  echo "   ✓ 本地已有镜像, 继续启动"
else
  echo "   ✓ 拉取完成"
fi

# ---- 3. 确保数据卷存在 ----
echo "💾 [3/5] 确保数据卷 $DATA_VOLUME 存在 (持久化配置/温度历史)..."
docker volume create "$DATA_VOLUME" >/dev/null 2>&1
echo "   ✓ 完成"

# ---- 4. 启动容器 (关键参数齐全, 杜绝 GUI 漏填坑) ----
echo "🚀 [4/5] 启动容器 $CONTAINER_NAME ..."
# 参数说明:
#   --user root           : root 用户 (NAS 访问串口/卷必须)
#   --privileged          : 特权模式 (自动获得所有 /dev 设备访问权)
#   --restart always      : NAS 重启后自动拉起
#   -p 8780:8780          : Web 控制台端口
#   -v smartfan-data:/data: 持久化卷 (配置 + 温度历史 JSONL)
#   -v /dev:/dev:rw       : 把宿主机全部设备映射进容器 (privileged 下串口直接能用)
#   -e TZ=Asia/Shanghai   : 时区 (曲线 X 轴时间正确)
#   --log-*               : 日志滚动限制 (10M × 5 文件)
#   --health-*            : 健康检查阈值 (start-period=90s, 给足首次 pip install 时间)
docker run -d \
  --name "$CONTAINER_NAME" \
  --user root \
  --privileged \
  --restart always \
  -p "$PORT:$PORT" \
  -v "$DATA_VOLUME":/data \
  -v /dev:/dev:rw \
  -e TZ="$TZ" \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=5 \
  --health-cmd "curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1 || exit 1" \
  --health-interval 20s \
  --health-timeout 5s \
  --health-start-period 90s \
  --health-retries 5 \
  "$IMAGE"

RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ docker run 启动失败! 常见原因:"
  echo "   • 端口 $PORT 已被占用? 请先停掉占用程序或换端口"
  echo "   • 设备 /dev 映射冲突? 请先删除旧容器再试"
  exit 3
fi
echo "   ✓ 容器已启动 (ID: $(docker ps -q --filter name=$CONTAINER_NAME | head -1))"

# ---- 5. 等待健康检查通过 ----
echo ""
echo "⏳ [5/5] 等待服务就绪 (首次启动需要补装依赖, 约 30~90 秒, 请耐心等待)..."
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$HOST_IP" ] && HOST_IP="<你的NAS-IP>"

for i in $(seq 1 24); do   # 24 × 5s = 最多等 2 分钟
  sleep 5
  STATUS=$(docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null)
  HEALTH=$(docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}' 2>/dev/null)
  case "$HEALTH" in
    healthy)
      echo ""
      echo "✅✅✅ 部署成功! 服务已健康运行!"
      echo "   浏览器打开: http://$HOST_IP:$PORT"
      echo "   容器状态: $STATUS / 健康: $HEALTH"
      echo "   查看日志: docker logs -f $CONTAINER_NAME"
      exit 0
      ;;
    unhealthy)
      echo "  $(date '+%H:%M:%S')  状态=$STATUS  健康=$HEALTH  (不健康, 正在重试检查)"
      if [ $i -ge 10 ]; then
        echo "  ⚠️  长时间不健康, 打印最新 20 行日志参考:"
        docker logs --tail 20 "$CONTAINER_NAME" 2>&1
      fi
      ;;
    *)
      echo "  $(date '+%H:%M:%S')  状态=$STATUS  健康=$HEALTH  (启动中...)"
      ;;
  esac
done

echo ""
echo "⏰ 超过 2 分钟仍未 healthy, 但容器仍在运行请稍候再访问..."
echo "   快速诊断命令:"
echo "     docker logs --tail 50 $CONTAINER_NAME   # 看启动日志有没有 WARNING"
echo "     docker inspect $CONTAINER_NAME --format '{{.State.ExitCode}}'  # 看退出码"
echo "   访问页面: http://$HOST_IP:$PORT"
exit 0
