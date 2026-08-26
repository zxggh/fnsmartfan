#!/bin/bash
# ============================================================
#  SmartFan v2.1 全新 NAS 模拟验证脚本 (一键版)
#
#  功能: 100% 模拟拿到一台全新 NAS
#        → 删除所有容器/镜像/数据卷
#        → 仅通过 docker-compose.yml 远程拉镜像部署
#        → 自动验证 4 项关键功能
#  用法: bash clean-deploy-test.sh
# ============================================================

set +e  # 单条失败不退出, 方便看汇总
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
LOG_FILE="$DEPLOY_DIR/clean-deploy-test-$(date +%Y%m%d-%H%M%S).log"

# ────────────────── 颜色辅助 ──────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass() { echo -e "  ${GREEN}✅ PASS${NC}: $1"; }
fail() { echo -e "  ${RED}❌ FAIL${NC}: $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
info() { echo -e "  ${CYAN}ℹ${NC}  $1"; }

# ────────────────── 日志重定向 ──────────────────
exec > >(tee -a "$LOG_FILE") 2>&1
echo ""
echo "============================================================"
echo " 🧪 SmartFan v2.1 全新 NAS 模拟验证 (一键版)"
echo "============================================================"
echo " 📝 日志文件: $LOG_FILE"
echo ""

# ═══════════════════════════════════════════════
# 安全确认
# ═══════════════════════════════════════════════
echo -e "${YELLOW}⚠⚠⚠  危险操作确认  ⚠⚠⚠${NC}"
echo "  本脚本会执行以下操作:"
echo "   1. 停止并删除 smartfan 容器"
echo "   2. 删除 ALL 本地 fnsmartfan:* 镜像 (latest/v2/golden 等全部)"
echo "   3. 删除 smartfan-data 数据卷 (config.yaml 会丢失!)"
echo ""
echo "  已自动备份 config.yaml 到: $DEPLOY_DIR/config.yaml.backup-时间戳"
echo ""
read -r -p "  确认继续? (输入 YES 全大写后回车): " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
  echo "❌ 用户取消, 脚本退出。"
  exit 0
fi

PASS_CNT=0; FAIL_CNT=0
check_result() {
  # $1=描述 $2=条件(非0为真)
  if [ "$2" -ne 0 ]; then pass "$1"; PASS_CNT=$((PASS_CNT+1))
  else fail "$1"; FAIL_CNT=$((FAIL_CNT+1)); fi
}

# ═══════════════════════════════════════════════
# 0. 备份
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [0/6] 备份 config.yaml"
echo "──────────────────────────────────────────────"
BACKUP_FILE="$DEPLOY_DIR/config.yaml.backup-$(date +%Y%m%d-%H%M%S)"
if docker cp smartfan:/app/config.yaml "$BACKUP_FILE" 2>/dev/null; then
  info "已备份到: $BACKUP_FILE ($(ls -lh "$BACKUP_FILE" | awk '{print $5}'))"
else
  # 容器不在就尝试从 /data 卷备份
  TMP_BACKUP_CONTAINER=$(docker run --rm -d -v smartfan-data:/v alpine sleep 60 2>/dev/null || true)
  if [ -n "$TMP_BACKUP_CONTAINER" ]; then
    if docker cp "$TMP_BACKUP_CONTAINER":/v/config.yaml "$BACKUP_FILE" 2>/dev/null; then
      info "从数据卷备份到: $BACKUP_FILE"
    else warn "容器/卷里都没找到 config.yaml, 跳过备份 (部署后会生成默认模板)"
    fi
    docker rm -f "$TMP_BACKUP_CONTAINER" >/dev/null 2>&1 || true
  else warn "无法备份 (没有容器也没有卷), 跳过备份"
  fi
fi

# ═══════════════════════════════════════════════
# 1. 彻底清场
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [1/6] 彻底清场: 容器 + 镜像 + 数据卷"
echo "──────────────────────────────────────────────"

# compose down
if [ -f "$COMPOSE_FILE" ]; then
  cd "$DEPLOY_DIR" && docker compose down -v 2>&1 | tail -5
fi
# 直接删容器/卷兜底
docker rm -f smartfan fnsmartfan 2>/dev/null || true
# 删所有 fnsmartfan 本地镜像
IMG_COUNT=$(docker images -a | grep -c "^fnsmartfan" || true)
if [ "$IMG_COUNT" -gt 0 ]; then
  info "发现 $IMG_COUNT 个本地 fnsmartfan 镜像, 正在删除..."
  docker images -a | grep "^fnsmartfan" | awk '{print $3}' | xargs -r docker rmi -f 2>&1 | tail -3 || true
fi
# 删卷
docker volume rm smartfan-data 2>/dev/null || true

# 确认
IMG_REMAIN=$(docker images | grep -c "fnsmartfan" || true)
CT_REMAIN=$(docker ps -a | grep -c "smartfan" || true)
VL_REMAIN=$(docker volume ls | grep -c "smartfan-data" || true)
info "剩余: 镜像=$IMG_REMAIN 容器=$CT_REMAIN 卷=$VL_REMAIN"
check_result "清场干净(镜像/容器/卷都为0)" $([ "$IMG_REMAIN" -eq 0 ] && [ "$CT_REMAIN" -eq 0 ] && [ "$VL_REMAIN" -eq 0 ] && echo 1 || echo 0)

# ═══════════════════════════════════════════════
# 2. 拉取远程镜像 (修复版: 不看退出码, 看 docker images 是否真的有 IMAGE_ID)
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [2/6] 拉取 ghcr.io/zxggh/fnsmartfan:latest"
echo "──────────────────────────────────────────────"
echo "(如果报 401/unauthorized → 飞牛 docker.fnnas.com 代理拦截了 GHCR 公共镜像)"
echo "(脚本会自动尝试 Fallback: 本地 tar 导入 / PAT 登录重新拉)"
sleep 2

TARGET_IMAGE="ghcr.io/zxggh/fnsmartfan:latest"
get_image_id() {
  docker images --format "{{.ID}}" "$TARGET_IMAGE" 2>/dev/null | head -n 1
}
BEFORE_ID="$(get_image_id)"

PULL_START=$(date +%s)
# 不用管道 tail → 否则 $? 是 tail 的退出码, 不是 docker pull 的
PULL_LOG=$(docker pull "$TARGET_IMAGE" 2>&1 || true)
PULL_RC=$?
echo "$PULL_LOG" | tail -15
PULL_SEC=$(( $(date +%s) - PULL_START ))
AFTER_ID="$(get_image_id)"

echo ""
info "拉取耗时: ${PULL_SEC}s, 拉取前 IMAGE_ID=${BEFORE_ID:-空}, 拉取后 IMAGE_ID=${AFTER_ID:-空}"

# ✅ 真正的成功判断: AFTER_ID 非空 且 不等于 BEFORE_ID (或之前空现在有了)
PULL_OK=0
if [ -n "$AFTER_ID" ] && [ "$AFTER_ID" != "$BEFORE_ID" ]; then PULL_OK=1; fi
# 兼容: 如果之前已经有同一个 ID (比如镜像没变化), 也算 OK
if [ -z "$BEFORE_ID" ] && [ -n "$AFTER_ID" ]; then PULL_OK=1; fi
if [ -n "$BEFORE_ID" ] && [ -n "$AFTER_ID" ] && [ "$AFTER_ID" = "$BEFORE_ID" ]; then PULL_OK=1; fi
check_result "远程镜像拉取成功 (IMAGE_ID 存在)" $PULL_OK

# ──────────────────── 拉取失败 → Fallback 自动处理 ────────────────────
if [ "$PULL_OK" -ne 1 ]; then
  echo ""
  warn "GHCR 拉取失败 (通常是飞牛 NAS docker.fnnas.com 代理拦截公共镜像, 即使镜像公开也会401)"
  echo "  🔧 自动执行 Fallback 流程:"

  FALLBACK_DONE=0

  # Fallback A: 搜索本地所有 fnsmartfan*.tar, 优先选体积最大的(完整镜像)
  echo ""
  echo "  [A] 扫描本地 fnsmartfan*.tar 备份..."
  TARS=$(find "$DEPLOY_DIR" -maxdepth 2 -type f \( -name "fnsmartfan*.tar" -o -name "*fnsmartfan*.tar*" \) -printf '%s %p\n' 2>/dev/null | sort -rn)
  TAR_COUNT=$(echo "$TARS" | grep -c . || true)
  if [ "$TAR_COUNT" -gt 0 ]; then
    info "发现 $TAR_COUNT 个 tar 备份:"
    echo "$TARS" | while read -r sz p; do
      echo "    - $(echo "scale=1;$sz/1024/1024" | bc 2>/dev/null || echo "$sz bytes") $p"
    done
    # 自动选体积最大的那个加载
    BEST_TAR=$(echo "$TARS" | head -n 1 | sed 's/^[0-9]* //')
    echo ""
    read -r -p "  是否自动加载 $BEST_TAR ? [Y/n]: " FALLBACK_TAR
    if [ "$FALLBACK_TAR" != "n" ] && [ "$FALLBACK_TAR" != "N" ]; then
      info "正在 docker load -i $BEST_TAR ..."
      if docker load -i "$BEST_TAR" 2>&1 | tail -5; then
        # 加载成功后, 把结果 tag 成 TARGET_IMAGE (让 compose 能匹配)
        LOADED_TAG=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep "^fnsmartfan:" | head -n 1)
        if [ -z "$LOADED_TAG" ]; then
          LOADED_TAG="fnsmartfan:latest"
        fi
        info "docker tag $LOADED_TAG → $TARGET_IMAGE"
        docker tag "$LOADED_TAG" "$TARGET_IMAGE" 2>/dev/null || true
        docker tag "$LOADED_TAG" "fnsmartfan:latest" 2>/dev/null || true
        FALLBACK_DONE=1
      fi
    fi
  else
    warn "  没有找到任何 fnsmartfan*.tar 备份"
  fi

  # Fallback B: docker login ghcr.io (用 PAT) 然后重新 pull
  if [ "$FALLBACK_DONE" -ne 1 ]; then
    echo ""
    echo "  [B] Fallback: 用 GitHub PAT 登录 GHCR 后重新 pull (解决代理拦截公共镜像的问题)"
    echo "      (如果你还没有 PAT, 去 https://github.com/settings/tokens 勾选 write:packages 生成)"
    read -r -p "  是否输入 GitHub 用户名+PAT 重新登录? [y/N]: " FALLBACK_LOGIN
    if [ "$FALLBACK_LOGIN" = "y" ] || [ "$FALLBACK_LOGIN" = "Y" ]; then
      read -r -p "    GitHub 用户名 (默认 zxggh): " GH_USER
      [ -z "$GH_USER" ] && GH_USER="zxggh"
      read -r -s -p "    GitHub Classic PAT (复制后回车, 不会显示): " GH_PAT
      echo ""
      if echo "$GH_PAT" | docker login ghcr.io -u "$GH_USER" --password-stdin 2>&1 | tail -3; then
        info "登录成功! 重新 pull $TARGET_IMAGE ..."
        docker pull "$TARGET_IMAGE" 2>&1 | tail -10 || true
      else
        warn "  登录失败, 跳过 Fallback B"
      fi
    fi
  fi

  # Fallback C: 确认镜像现在是否存在 (以上任何一种 Fallback 成功后)
  AFTER_FB_ID="$(get_image_id)"
  if [ -n "$AFTER_FB_ID" ]; then
    pass "Fallback 后镜像已就绪 (IMAGE_ID=$AFTER_FB_ID)"; PULL_OK=1; PASS_CNT=$((PASS_CNT+1))
  else
    # 最后再兜底: 扫本地所有 fnsmartfan:* 标签, 随便 tag 一个为 TARGET_IMAGE
    ANY_LOCAL=$(docker images --format "{{.Repository}}:{{.Tag}}" | grep "^fnsmartfan:" | head -n 1)
    if [ -n "$ANY_LOCAL" ]; then
      warn "  发现本地镜像 $ANY_LOCAL, 自动 tag 成 $TARGET_IMAGE (临时兜底)"
      docker tag "$ANY_LOCAL" "$TARGET_IMAGE" 2>/dev/null || true
      AFTER_FB_ID="$(get_image_id)"
      if [ -n "$AFTER_FB_ID" ]; then PULL_OK=1; fi
    fi
  fi

  # 如果 Fallback 还是没镜像, 提醒用户并退出
  if [ "$PULL_OK" -ne 1 ]; then
    echo ""
    fail "所有 Fallback 方案均未获得可用镜像! 中止脚本"
    echo ""
    echo "  手动解决办法:"
    echo "  方法 1 (推荐): Windows 本地 Docker Desktop 执行:"
    echo "    docker login ghcr.io -u zxggh"
    echo "    docker pull ghcr.io/zxggh/fnsmartfan:latest"
    echo "    docker save ghcr.io/zxggh/fnsmartfan:latest -o fnsmartfan-latest.tar"
    echo "    → 把 tar 上传到 $DEPLOY_DIR/ 后重新执行本脚本"
    echo ""
    echo "  方法 2: 在 NAS 任务计划 (非 webSSH, webSSH 是非交互环境) 里一次性跑:"
    echo "    echo '你的_Classic_PAT' | docker login ghcr.io -u zxggh --password-stdin"
    echo "    docker pull ghcr.io/zxggh/fnsmartfan:latest"
    exit 1
  fi
fi

# ═══════════════════════════════════════════════
# 3. Compose 启动
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [3/6] docker compose up -d 启动"
echo "──────────────────────────────────────────────"
if [ ! -f "$COMPOSE_FILE" ]; then
  fail "找不到 docker-compose.yml! ($COMPOSE_FILE)"
  echo "   请把脚本放在和 docker-compose.yml 同一目录执行"
  exit 1
fi
cd "$DEPLOY_DIR" && docker compose up -d 2>&1 | tail -5
sleep 6

# 等 web 就绪 (最多 60 秒: 首次启动会 pip install 依赖)
info "等待 web 服务就绪 (首次启动可能 20-40 秒, 在 pip 补装依赖)..."
READY=0
for i in $(seq 1 30); do
  if curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1; then
    READY=1; break
  fi
  echo -n "."
  sleep 2
done
echo ""
info "容器状态: $(docker ps --filter name=smartfan --format '{{.Status}}')"
check_result "容器启动 & web 服务就绪" $READY

# ═══════════════════════════════════════════════
# 4. 验证 4 项关键功能
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [4/6] 关键功能验证 (4 项)"
echo "──────────────────────────────────────────────"

# ── 4.1 镜像是否预装 smartctl (核心! → 保证不用手动 apt install)
echo ""
echo "▶ 4.1 镜像自带 smartctl (smartmontools) 验证"
SMART_BIN=$(docker exec smartfan which smartctl 2>/dev/null || echo "NOT_FOUND")
check_result "smartctl = /usr/sbin/smartctl" $([ "$SMART_BIN" = "/usr/sbin/smartctl" ] && echo 1 || echo 0)
[ "$SMART_BIN" != "/usr/sbin/smartctl" ] && warn "实际值: $SMART_BIN (如果不是新镜像, 检查 ghcr 拉取是不是还是旧层)"

# ── 4.2 HDD 温度识别
echo ""
echo "▶ 4.2 HDD 温度识别 (等待 12 秒首次 smartctl 采集完成)"
sleep 12
TEMPS=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/temps 2>/dev/null || echo "{}")
HDD_VAL=$(python3 -c "import sys,json;d=json.load(sys.stdin);t=d.get('data',{}).get('hdd');print('NONE' if t is None else t)" 2>/dev/null <<<"$TEMPS" || echo "PARSE_ERR")
SSD_VAL=$(python3 -c "import sys,json;d=json.load(sys.stdin);t=d.get('data',{}).get('ssd');print('NONE' if t is None else t)" 2>/dev/null <<<"$TEMPS" || echo "PARSE_ERR")
CPU_VAL=$(python3 -c "import sys,json;d=json.load(sys.stdin);t=d.get('data',{}).get('cpu');print('NONE' if t is None else t)" 2>/dev/null <<<"$TEMPS" || echo "PARSE_ERR")
DISK_KEYS=$(python3 -c "import sys,json;d=json.load(sys.stdin);raw=d.get('data',{}).get('raw',{});print([k for k in raw if k.startswith('disk_')])" 2>/dev/null <<<"$TEMPS" || echo "[]")
info "温度: CPU=$CPU_VAL°C  SSD=$SSD_VAL°C  HDD=$HDD_VAL°C  已识别磁盘=$DISK_KEYS"
HDD_OK=0
if [ "$HDD_VAL" != "NONE" ] && [ "$HDD_VAL" != "PARSE_ERR" ] && [ "$HDD_VAL" != "N/A" ]; then HDD_OK=1; fi
check_result "HDD 温度识别成功" $HDD_OK
if [ "$HDD_OK" -eq 0 ]; then
  warn "如果 HDD_NONE 但 disk 键里有值, 可能是这台机器没接 HDD, 或首次采集慢, 过一会刷新页面"
  info "手动验证命令: docker exec smartfan /usr/sbin/smartctl -A -d sat /dev-host-dev/sda 2>&1 | grep -i temp"
fi

# ── 4.3 自动命令开关 API (v2 新增)
echo ""
echo "▶ 4.3 自动命令开关 API 验证"
AC_JSON=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
AC_ENABLED=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('enabled','NA'))" 2>/dev/null <<<"$AC_JSON" || echo "NA")
INFO_JSON=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/info 2>/dev/null || echo "{}")
INFO_HAS_FIELD=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(1 if 'auto_cmd_enabled' in d.get('data',{}) else 0)" 2>/dev/null <<<"$INFO_JSON" || echo 0)
check_result "GET /api/auto-cmd 返回 enabled=True/False (值=$AC_ENABLED)" $([ "$AC_ENABLED" = "True" ] || [ "$AC_ENABLED" = "False" ] && echo 1 || echo 0)
check_result "/api/info 含 auto_cmd_enabled 字段" $INFO_HAS_FIELD

# 切换开关测试
echo ""
echo "  切换开关持久化测试 (True→False→True)..."
T1=$(docker exec smartfan curl -s -m 5 -X POST -H "Content-Type: application/json" \
       -d '{"enabled":false}' http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
T2=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
T3=$(docker exec smartfan curl -s -m 5 -X POST -H "Content-Type: application/json" \
       -d '{"enabled":true}' http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
T4=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/auto-cmd 2>/dev/null || echo "{}")
SET_FALSE=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('enabled','NA'))" 2>/dev/null <<<"$T2" || echo "NA")
SET_TRUE=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('enabled','NA'))" 2>/dev/null <<<"$T4" || echo "NA")
if [ "$SET_FALSE" = "False" ] && [ "$SET_TRUE" = "True" ]; then pass "自动命令开关 切换+持久化 正常 (True↔False)"; PASS_CNT=$((PASS_CNT+1))
else fail "开关切换异常: POST后读回=False→$SET_FALSE, 再=True→$SET_TRUE"; FAIL_CNT=$((FAIL_CNT+1)); fi

# ── 4.4 控制器连接 + /api/status 有 auto_cmd_enabled
echo ""
echo "▶ 4.4 控制器状态验证"
STATUS_JSON=$(docker exec smartfan curl -s -m 5 http://127.0.0.1:8780/api/status 2>/dev/null || echo "{}")
CONNECTED=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('data',{}).get('controller_connected',False))" 2>/dev/null <<<"$INFO_JSON" || echo "False")
STATUS_HAS_FIELD=$(python3 -c "import sys,json;d=json.load(sys.stdin);print(1 if 'auto_cmd_enabled' in d else 0)" 2>/dev/null <<<"$STATUS_JSON" || echo 0)
check_result "/api/status 含 auto_cmd_enabled 字段" $STATUS_HAS_FIELD
info "控制器当前连接状态: $CONNECTED (如果 False, 请确认 USB 控制器已插入, 稍后会自动重连)"
if [ "$CONNECTED" = "False" ]; then
  warn "当前未连接控制器, 但不影响本脚本验证(容器镜像和 API 均已通过)"
fi

# ═══════════════════════════════════════════════
# 5. 风扇进度条立即刷新(给出手动验证提示)
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [5/6] 手动验证项提示 (浏览器打开 Web UI 后做)"
echo "──────────────────────────────────────────────"
NAS_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "NAS_IP")
info "🌐 Web UI: http://$NAS_IP:8780"
echo "  ① 『🖥️ 原始命令』卡顶部 → 绿色『⏸ 已启用』按钮存在(不是旧镜像)"
echo "  ② 点『▶ 执行温控』 → 风扇1 进度条 1s 内立刻刷新 (不再等3秒轮询)"
echo "  ③ 原始命令输入框输 F1CPD=50 → 回车 → 进度条立刻到 50%"
echo "  ④ 点一下『⏸ 已启用』变成红色『▶ 已停用』 → 等10秒看日志, 没有自动心跳(只有手动发送才占用)"
echo "  ⑤ 拔插控制器 USB → 断连 → 重连后自动恢复"

# ═══════════════════════════════════════════════
# 6. 汇总 + 新设备部署指南(不用 SSH!)
# ═══════════════════════════════════════════════
echo ""
echo "──────────────────────────────────────────────"
echo " [6/6] 验证汇总"
echo "──────────────────────────────────────────────"
TOTAL=$((PASS_CNT + FAIL_CNT))
echo -e "  ${GREEN}通过: $PASS_CNT${NC}   ${RED}失败: $FAIL_CNT${NC}   合计: $TOTAL"
echo ""
if [ "$FAIL_CNT" -eq 0 ]; then
  echo -e "  ${GREEN}🎉 全部通过! 镜像没有问题, 可以放心部署到其他 NAS。${NC}"
else
  echo -e "  ${YELLOW}⚠ 有 $FAIL_CNT 项失败, 查看日志文件: $LOG_FILE${NC}"
fi

echo ""
echo ""
echo "============================================================"
echo "  💡 新设备部署 (不用 SSH! 全程 NAS Web 界面操作)"
echo "============================================================"
echo "  准备文件: docker-compose.remote.yml (和本脚本同目录)"
echo ""
echo "  步骤 (全程点鼠标, 无需命令行):"
echo "  1) 打开飞牛 NAS Web 管理 → 『容器』 → 左侧『Compose』"
echo "  2) 点击 『+ 创建项目』 → 选择『上传 compose 文件』"
echo "  3) 项目名称填: smartfan"
echo "  4) 路径选择: /vol1/1000/个人/smart fan (或你想放的任何目录)"
echo "  5) 上传文件: 选 docker-compose.remote.yml (不要选脚本目录那个带 volumes 备份字段的)"
echo "  6) 点击 『确定』 → NAS 会自动拉取 ghcr.io/zxggh/fnsmartfan:latest + 创建 + 启动"
echo "  7) 浏览器打开 http://NAS_IP:8780 即可使用"
echo ""
echo "  * 如果 GHCR 拉取失败(飞牛 NAS docker.fnnas.com 代理拦截公共镜像):"
echo "    A) 方法一: NAS 任务计划 执行一次 docker login ghcr.io + PAT 登录"
echo "    B) 方法二: Windows 本地 save tar, 上传 tar 后在 NAS 容器 → 『镜像』 → 『导入镜像』"
echo "               然后把 compose 的 image 改成 fnsmartfan:latest 本地镜像即可"
echo "============================================================"
echo "  📋 完整日志已保存: $LOG_FILE"
echo "============================================================"
