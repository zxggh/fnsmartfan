#!/bin/bash
# ==============================================
#  SmartFan v2 补丁应用脚本
#  功能: 把修改后的5个文件拷进容器 → 重启容器 → 验证功能
#  用法: 把本脚本和 "修改后文件_v2" 文件夹放在 NAS 同一目录下, 然后:
#        cd /path/to/files
#        bash apply-patch-v2.sh
# ==============================================

set -e
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)/修改后文件_v2"
CONTAINER="smartfan"

echo "========================================"
echo "  SmartFan v2 补丁应用"
echo "========================================"

# 1. 检查补丁目录
if [ ! -d "$PATCH_DIR" ]; then
  echo "❌ 找不到补丁目录: $PATCH_DIR"
  echo "   请确保本脚本和 '修改后文件_v2' 文件夹在同一目录下"
  exit 1
fi
echo "✅ 补丁目录: $PATCH_DIR"

# 2. 检查容器是否在运行
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER}$"; then
  echo "⚠ 容器 $CONTAINER 未运行, 尝试启动..."
  docker start $CONTAINER 2>/dev/null || true
  sleep 3
fi
if ! docker ps --format "{{.Names}}" | grep -q "^${CONTAINER}$"; then
  echo "❌ 容器 $CONTAINER 无法启动, 请先确保容器可用"
  exit 1
fi
echo "✅ 容器 $CONTAINER 正在运行"

# 3. 拷贝文件进容器 (先备份原文件到 /data/backup-v1)
echo ""
echo "[1/4] 备份原文件 + 拷贝新文件进容器..."
docker exec $CONTAINER bash -c "mkdir -p /data/backup-v1 && \
  cp /app/main.py /data/backup-v1/ 2>/dev/null || true && \
  cp /app/temp_collector.py /data/backup-v1/ 2>/dev/null || true && \
  cp -r /app/static /data/backup-v1/ 2>/dev/null || true && \
  echo '原文件已备份到 /data/backup-v1'"

docker cp "$PATCH_DIR/main.py"           $CONTAINER:/app/main.py
docker cp "$PATCH_DIR/temp_collector.py" $CONTAINER:/app/temp_collector.py
docker cp "$PATCH_DIR/static/index.html" $CONTAINER:/app/static/index.html
docker cp "$PATCH_DIR/static/app.js"     $CONTAINER:/app/static/app.js
docker cp "$PATCH_DIR/static/style.css"  $CONTAINER:/app/static/style.css
echo "✅ 5 个文件已拷贝进 /app"

# 4. 重启容器
echo ""
echo "[2/4] 重启容器使修改生效..."
docker restart $CONTAINER
sleep 5
# 等待服务就绪(最多等30秒)
for i in $(seq 1 15); do
  if docker exec $CONTAINER curl -s -m 2 http://127.0.0.1:8780/api/info >/dev/null 2>&1; then
    echo "✅ 服务已就绪"
    break
  fi
  echo -n "."
  sleep 2
done

# 5. 验证自动命令开关 API
echo ""
echo "[3/4] 验证功能..."
echo -n "  自动命令开关 API: "
AUTO_CMD=$(docker exec $CONTAINER curl -s -m 5 http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
if echo "$AUTO_CMD" | grep -q "enabled"; then
  echo "✅ $AUTO_CMD"
else
  echo "⚠ 返回异常: $AUTO_CMD"
fi

echo -n "  /api/info 带开关状态: "
INFO=$(docker exec $CONTAINER curl -s -m 5 http://127.0.0.1:8780/api/info 2>/dev/null || echo "{}")
if echo "$INFO" | grep -q "auto_cmd_enabled"; then
  echo "✅ (已包含 auto_cmd_enabled 字段)"
else
  echo "⚠ 未找到 auto_cmd_enabled 字段"
fi

# 6. 显示温度采集结果
echo ""
echo "[4/4] 查看当前温度 (确认 HDD 是否有值):"
TEMPS=$(docker exec $CONTAINER curl -s -m 5 http://127.0.0.1:8780/api/temps 2>/dev/null || echo "{}")
CPU=$(echo "$TEMPS"  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('data',{}).get('cpu','N/A'))" 2>/dev/null || echo "N/A")
SSD=$(echo "$TEMPS"  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('data',{}).get('ssd','N/A'))" 2>/dev/null || echo "N/A")
HDD=$(echo "$TEMPS"  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('data',{}).get('hdd','N/A'))" 2>/dev/null || echo "N/A")
echo "  CPU: $CPU °C"
echo "  SSD: $SSD °C"
echo "  HDD: $HDD °C"
if [ "$HDD" != "N/A" ] && [ "$HDD" != "None" ]; then
  echo "  ✅ HDD 温度已识别! 如果不显示, 等 10-15 秒让 smartctl 采集完成后刷新页面"
else
  echo "  ⚠ HDD 温度暂无值, 可能原因:"
  echo "    1. smartctl 首次采集还没完成, 等 10-15 秒刷新页面重试"
  echo "    2. 容器里没有 smartctl, 需要安装: docker exec smartfan apt-get update && apt-get install -y smartmontools"
  echo "    3. 磁盘设备未映射, 检查 /dev-host-dev/sd* 是否存在: docker exec smartfan ls -l /dev-host-dev/sd* 2>&1"
fi

echo ""
echo "========================================"
echo "  ✅ 补丁应用完成!"
echo "========================================"
echo ""
echo " 🌐 访问 Web UI: http://NAS_IP:8780"
echo "   - 在 '原始命令' 卡片顶部查看 自动命令 开关"
echo "   - 关闭后: 系统停止自动心跳/温控下发, 但仍允许手动发送命令 (调试用)"
echo "   - 开启后: 恢复正常运行"
echo ""
echo " 📋 调试用 API:"
echo "   GET  /api/auto-cmd          查询开关状态"
echo "   POST /api/auto-cmd          {\"enabled\":true/false} 切换开关"
echo "   GET  /api/temps             查看温度 (hdd 字段)"
echo ""
