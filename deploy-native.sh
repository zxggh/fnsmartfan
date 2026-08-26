#!/bin/bash
# ============================================================
# SmartFan 智能风扇控制器 —— 飞牛NAS 一键原生部署脚本
# 零Docker、零SSH，在 NAS 后台的「任务计划/自定义脚本」里执行一次即可
#
# 功能：
#   1. 从 smartfan-install.sh 中提取项目代码并解压到当前目录
#   2. 用 NAS 自带的 Python 3 建虚拟环境
#   3. 安装 pyserial-asyncio / fastapi / uvicorn 等依赖
#   4. 添加开机自启（crontab @reboot + 延时30秒启动）
#   5. 立即启动服务
# ============================================================

set -e

# ---------- 1. 定位部署目录（脚本所在位置）----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================================"
echo "  SmartFan 智能风扇控制器 - 一键原生部署工具"
echo "============================================================"
echo ""
echo "部署目录: $SCRIPT_DIR"
echo ""

# ---------- 2. 检查 smartfan-install.sh 是否存在 ----------
if [ ! -f "smartfan-install.sh" ]; then
    echo "❌ 错误：当前目录未找到 smartfan-install.sh"
    echo "请把 smartfan-install.sh 和本脚本放在同一个文件夹后再运行"
    exit 1
fi
echo "✅ 检测到 smartfan-install.sh"

# ---------- 3. 检查 Python 3 ----------
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 错误：系统未安装 python3"
    echo "请先在飞牛应用中心安装 Python 3.1，安装完成后再运行本脚本"
    exit 1
fi
PYVER="$(python3 -c 'import sys; print("%d.%d"%(sys.version_info.major,sys.version_info.minor))')"
echo "✅ 检测到 Python 版本: $PYVER"

# ---------- 4. 提取 BASE64_DATA 并解压 ----------
echo ""
echo "[1/5] 从安装脚本提取项目代码..."

# 提取 BASE64_DATA 引号内的内容 → 解码 → 解压到当前目录
PROJECT_BASE64="$(sed -n 's/^BASE64_DATA="\(.*\)"/\1/p' smartfan-install.sh)"
if [ -z "$PROJECT_BASE64" ]; then
    echo "❌ 错误：无法从 smartfan-install.sh 中提取 BASE64_DATA"
    exit 1
fi

TMP_TAR="$(mktemp /tmp/smartfan-XXXXXX.tar.gz)"
echo "$PROJECT_BASE64" | base64 -d > "$TMP_TAR"
tar xzf "$TMP_TAR" -C "$SCRIPT_DIR"
rm -f "$TMP_TAR"

# 如果解压后是 fan-controller 子目录，把内容移出来
if [ -d "$SCRIPT_DIR/fan-controller" ]; then
    (cd "$SCRIPT_DIR/fan-controller" && cp -a . "$SCRIPT_DIR/")
    rm -rf "$SCRIPT_DIR/fan-controller"
fi

echo "✅ 项目代码已解压完成"

# ---------- 5. 创建 Python 虚拟环境并安装依赖 ----------
echo ""
echo "[2/5] 创建 Python 虚拟环境并安装依赖（阿里云+清华源加速）..."

# 建 venv
if [ ! -d "venv" ]; then
    python3 -m venv --system-site-packages venv 2>/dev/null \
    || python3 -m venv venv
fi

VENV_PY="$SCRIPT_DIR/venv/bin/python"
VENV_PIP="$SCRIPT_DIR/venv/bin/pip"

if [ ! -f "$VENV_PIP" ]; then
    echo "❌ 错误：虚拟环境创建失败，找不到 pip"
    exit 1
fi

# 升级 pip 后安装依赖（清华 PyPI 源）
"$VENV_PIP" install --upgrade pip --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1 || true
"$VENV_PIP" install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    pyserial-asyncio fastapi uvicorn

echo "✅ 依赖安装完成"

# ---------- 6. 确保启动脚本可执行，并生成一个统一的启动包装脚本 ----------
echo ""
echo "[3/5] 准备启动脚本..."
chmod +x "$SCRIPT_DIR/start.sh" 2>/dev/null || true

cat > "$SCRIPT_DIR/run-smartfan.sh" << 'WRAPPER_EOF'
#!/bin/bash
# SmartFan 统一启动包装脚本（自启和手动启动都调这个）
cd "$(dirname "$0")"

# 给串口设备开放权限（STC 控制器一般是 /dev/ttyACM*）
for port in /dev/ttyACM*; do
    [ -e "$port" ] && chmod 666 "$port" 2>/dev/null || true
done

# 启动服务（把日志写到 log.txt，方便排错）
exec ./venv/bin/python start.sh > "$(dirname "$0")/log.txt" 2>&1
WRAPPER_EOF
chmod +x "$SCRIPT_DIR/run-smartfan.sh"

echo "✅ 启动脚本已就绪: run-smartfan.sh"

# ---------- 7. 添加开机自启（crontab @reboot）----------
echo ""
echo "[4/5] 配置开机自启..."

CRON_LINE="@reboot sleep 30; cd \"$SCRIPT_DIR\" && ./run-smartfan.sh"

# 把当前 crontab 取出来，去掉旧的 smartfan 行，再追加新的一行
( crontab -l 2>/dev/null | grep -v "smartfan\|run-smartfan\|fan-controller" ; echo "$CRON_LINE" ) | crontab -

echo "✅ 开机自启已加入 crontab（开机 30 秒后自动启动）"

# ---------- 8. 立即启动 ----------
echo ""
echo "[5/5] 立即启动服务..."

# 先停掉可能已经在跑的老实例
pkill -f "start.sh" 2>/dev/null || true
pkill -f "uvicorn" 2>/dev/null || true
sleep 1

# 后台启动
nohup "$SCRIPT_DIR/run-smartfan.sh" &>/dev/null &
echo $! > "$SCRIPT_DIR/smartfan.pid"
disown 2>/dev/null || true

sleep 5

# 简单检查进程是否还在
if ps -p "$(cat "$SCRIPT_DIR/smartfan.pid" 2>/dev/null)" >/dev/null 2>&1; then
    echo "✅ 服务启动成功 (PID: $(cat smartfan.pid))"
else
    # 进程没活下来就检查一下 log 给提示
    if [ -f log.txt ]; then
        echo "⚠️  启动后进程异常退出，请把以下日志粘贴给技术支持："
        echo "---------- log.txt 最后 20 行 ----------"
        tail -n 20 log.txt
        echo "--------------------------------------"
    else
        echo "⚠️  未能确认服务状态，稍后访问 http://IP:8780 确认"
    fi
fi

# ---------- 9. 完成提示 ----------
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo "============================================================"
echo "  ✅ 全部部署完成！"
echo "============================================================"
echo ""
echo "  Web 控制台：http://${HOST_IP:-<你的NAS内网IP>}:8780"
echo ""
echo "  常用管理命令（SSH 登录后用）："
echo "    查看日志  : cd $SCRIPT_DIR && tail -f log.txt"
echo "    手动启动  : cd $SCRIPT_DIR && ./run-smartfan.sh &"
echo "    停止服务  : pkill -f run-smartfan.sh"
echo "    重启服务  : pkill -f run-smartfan.sh; cd $SCRIPT_DIR && ./run-smartfan.sh &"
echo ""
