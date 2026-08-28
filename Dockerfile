# ============================================================
# STC 智能风扇控制器 Docker 镜像
#
# 标准部署流程（飞牛NAS，无401）：
#   1. NAS 容器界面手动拉取下面其中一个 Python 镜像（带飞牛鉴权，不会401）
#   2. SSH 执行 ./build-on-nas.sh（--pull=never 强制只用本地镜像）
#   3. docker compose -f docker-compose-offline.yml up -d
#
# Python 版本切换：取消注释你想用的一条 FROM，其余保持注释
#   版本选择建议：
#     - python:3.10-slim  ← 推荐（和NAS应用中心版本一致，最稳定）
#     - python:3.11-slim  ← 性能更好（约5~10%），需确认飞牛有该标签
#     - python:3.12-slim  ← 最新版，兼容性请自行评估
# ============================================================

# 【版本A：Python 3.10（推荐首选，最稳妥）】
FROM python:3.10-slim

# 【版本B：Python 3.11（性能升级，可选）】
# FROM python:3.11-slim

# 【版本C：Python 3.12（最新版，谨慎使用）】
# FROM python:3.12-slim

# 作者与描述信息
LABEL maintainer="SmartFan Controller"
LABEL description="SmartFan Controller Docker Image"

# 避免 debconf 报错(非交互安装) + 国内 apt 源 + 预装 smartmontools
#  smartmontools 提供 /usr/sbin/smartctl，用于采集 HDD 温度
#  DEBIAN_FRONTEND=noninteractive 防止 "Please select the geographic area..." 等交互弹窗
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
 || sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null \
 || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    coreutils \
    tar \
    sudo \
    curl \
    ca-certificates \
    smartmontools \
 && rm -rf /var/lib/apt/lists/* \
 && which smartctl || echo "smartctl install check ok"

# 设置工作目录
WORKDIR /app

# 将安装脚本复制到镜像中（不修改源文件）
COPY smartfan-install.sh /tmp/install.sh

# 从安装脚本中提取 BASE64_DATA 并解压项目文件
# 原理：匹配 BASE64_DATA="..." 行，提取引号之间的内容，解码并解压
RUN set -e \
 && mkdir -p /app \
 && sed -n 's/^BASE64_DATA="\(.*\)"/\1/p' /tmp/install.sh | base64 -d | tar xzf - -C /app \
 && rm -f /tmp/install.sh

# 项目解压后的目录名为 fan-controller，将其内容移动到 /app
# 如果解压后正好是 fan-controller 子目录，则移动内容
RUN set -e \
 && if [ -d /app/fan-controller ]; then \
      mv /app/fan-controller/* /app/ 2>/dev/null || true; \
      mv /app/fan-controller/.* /app/ 2>/dev/null || true; \
      rmdir /app/fan-controller 2>/dev/null || true; \
    fi

# 创建 Python 虚拟环境（与原安装脚本保持一致）
RUN python3 -m venv --system-site-packages /app/venv 2>/dev/null \
 || python3 -m venv /app/venv

# 安装 Python 依赖
# ★ v6 修复 GitHub Actions 构建失败(pyserial-asyncio 找不到):
#   1. 只用清华 PyPI(中国镜像)在 Actions runner(美国)搜不到小众包 pyserial-asyncio,
#      改为「官方 PyPI 为主 + 清华 extra 为辅」双源 + 5 次重试,
#      在任何地区构建都能找到包, 国内用户也有加速.
#   2. 统一用 venv/bin/python -m pip 代替裸 pip, 符合 venv 最佳实践,
#      避免某些 slim 镜像里 pip shebang 找不到的问题.
#   3. 第二个 pip 末尾加 || true: 构建阶段即使偶发失败也不中断,
#      因为 CMD 启动时会再补装一次(见底部启动命令).
RUN /app/venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true \
 && /app/venv/bin/python -m pip install \
      --no-cache-dir --retries 5 --timeout 60 \
      --index-url https://pypi.org/simple/ \
      --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      --trusted-host pypi.org --trusted-host pypi.python.org \
      --trusted-host files.pythonhosted.org \
      --trusted-host pypi.tuna.tsinghua.edu.cn \
      -r /app/requirements.txt 2>/dev/null || true \
 && /app/venv/bin/python -m pip install \
      --no-cache-dir --retries 5 --timeout 60 \
      --index-url https://pypi.org/simple/ \
      --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      --trusted-host pypi.org --trusted-host pypi.python.org \
      --trusted-host files.pythonhosted.org \
      --trusted-host pypi.tuna.tsinghua.edu.cn \
      pyserial-asyncio fastapi uvicorn PyYAML || true

# 确保启动脚本有执行权限
RUN chmod +x /app/start.sh 2>/dev/null || true

# 创建非 root 用户运行服务（安全最佳实践）
RUN groupadd -r dialout 2>/dev/null || true \
 && useradd -r -u 1000 -G dialout fanuser \
 && chown -R fanuser:fanuser /app

# ============================================================
# 调试辅助：构建阶段列出 /app 内容，方便确认解压结果
# 如果启动时找不到 start.sh，看构建日志里这一段就能排错
# ============================================================
RUN echo "--- /app contents after build ---" && ls -la /app && echo "--- start.sh exists:" && cat /app/start.sh 2>/dev/null | head -50 || echo "start.sh missing"

# 切换到普通用户 — NAS 部署场景下默认关闭, 保持 root 启动
# ★ v6 修复无限重启: 飞牛/群晖等 NAS 的 Docker 挂载卷默认所有者是 root(uid 0),
#   如果用非 root 用户(fanuser uid 1000)启动, 写入 /data/config.yaml 或
#   /data/temp_history.jsonl 会触发 PermissionError → FastAPI 启动失败 →
#   容器退出 → 重启策略触发 → 无限重启循环.
#   用户容器配置中已勾选「最高权限」(特权模式), 保持 root 启动是 NAS
#   类环境的事实标准(同时能正确访问 /dev/ttyUSB* /dev/sd* 等设备).
# USER fanuser

# 暴露 Web 控制台端口
EXPOSE 8780

# 健康检查：通过 /api/info 接口判断服务状态
# ★ v6 修复飞牛 GUI 误判「启动失败」坑:
#   首次启动要 pip install 补装依赖约 30~60s, 之前 start-period=10s 太短,
#   飞牛 GUI 会在前 30s 内看到不健康就直接判定失败.
#   改为: start-period=90s + interval=20s + retries=5,
#   给足首次启动补装依赖 + 预热时间, GUI 不会误判.
HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1 || exit 1

# 启动命令(★ v6 防无限重启版 + NAS 部署环境自检):
#  1) 不用 set -e 也不用 && 链式: 任何一步失败都不立刻退出容器,
#     而是打印错误并继续, 启动失败也 sleep 10 秒方便定位.
#  2) 启动前做环境自检(UID / 串口设备 / 卷写权限), 不满足时
#     打印中文 ERROR 级修复指引, 用户 docker logs 一眼能看到.
CMD ["bash", "-c", "\
echo '[smartfan-init] ==== 容器启动, 开始初始化 ===='; \
echo '[smartfan-init] 环境自检:'; \
uid=$(id -u); \
echo \"  当前运行用户 UID: $uid\"; \
if [ \"$uid\" != \"0\" ]; then \
  echo '  ⚠️  WARNING: 非 root 用户启动! NAS 上访问串口/卷大概率失败, 请加 --user root 或在 GUI 创建容器时切换到 root 用户'; \
fi; \
tty_cnt=$(ls /dev/ttyUSB* /dev/ttyACM* /dev-host-dev/ttyUSB* /dev-host-dev/ttyACM* 2>/dev/null | wc -l); \
echo \"  检测到的串口设备数: $tty_cnt\"; \
if [ \"$tty_cnt\" -eq 0 ] && [ ! -d /dev/usbmon0 ] 2>/dev/null; then \
  echo '  ⚠️  WARNING: 未检测到任何串口设备! 请确保启动时加 --privileged 并映射 /dev (推荐) 或逐个 --device /dev/ttyUSB0 等'; \
fi; \
mkdir -p /data 2>/dev/null; \
if touch /data/.write_test 2>/dev/null; then \
  echo '  /data 卷写入权限: ✓ OK'; \
  rm -f /data/.write_test; \
else \
  echo '  ⚠️  WARNING: /data 卷无写入权限! 温度历史/配置持久化失效, 请执行: chmod -R 777 /你的卷路径'; \
fi; \
cd /app || (echo '[smartfan-init] ERROR: cd /app 失败' && sleep 10 && exit 1); \
echo '[smartfan-init] 1/5 确保 /data 卷目录存在且可写'; \
chmod 777 /data 2>/dev/null || echo '[smartfan-init] WARN: chmod /data 失败(继续)'; \
echo '[smartfan-init] 2/5 config.yaml 初始化(空卷复制模板, 软链到 /app)'; \
if [ ! -f /data/config.yaml ] && [ -f /app/config.yaml ]; then \
  cp /app/config.yaml /data/config.yaml && echo '[smartfan-init] OK: 模板 config.yaml 复制到卷' \
  || echo '[smartfan-init] WARN: 复制 config.yaml 失败(继续使用默认配置)'; \
fi; \
if [ -f /data/config.yaml ]; then chmod 666 /data/config.yaml 2>/dev/null || true; fi; \
rm -f /app/config.yaml 2>/dev/null || true; \
ln -sf /data/config.yaml /app/config.yaml 2>/dev/null || echo '[smartfan-init] WARN: 软链 config.yaml 失败(继续)'; \
echo '[smartfan-init] 3/5 确保 pip 可用 + 补装依赖(首次启动较慢, 请耐心等待, 通常 30~60 秒)'; \
/app/venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true; \
/app/venv/bin/python -m pip install --progress-bar off --no-cache-dir \
  --retries 5 --timeout 60 \
  --index-url https://pypi.org/simple/ \
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  --trusted-host pypi.org --trusted-host pypi.python.org \
  --trusted-host files.pythonhosted.org \
  --trusted-host pypi.tuna.tsinghua.edu.cn \
  -r /app/requirements.txt PyYAML pyserial pyserial-asyncio fastapi uvicorn schedule \
  >/tmp/pip.log 2>&1 && echo '[smartfan-init] OK: 依赖检查完成' \
  || { echo '[smartfan-init] WARN: 依赖安装失败, 打印 pip.log 末尾 20 行:'; tail -20 /tmp/pip.log; }; \
echo '[smartfan-init] 4/5 启动前检查 main.py 和 Python 环境'; \
/app/venv/bin/python -c 'import fastapi, uvicorn, serial_asyncio, yaml, schedule; print(\"  deps ok: fastapi+\", fastapi.__version__, sep=\"\")' 2>/dev/null \
  || echo '[smartfan-init] WARN: 依赖导入测试失败(可能影响功能, 继续尝试启动)'; \
if [ ! -f /app/main.py ]; then echo '[smartfan-init] ERROR: /app/main.py 不存在! 打包/解压失败'; sleep 10; exit 2; fi; \
echo '[smartfan-init] 5/5 启动 uvicorn (端口 8780), 服务日志如下:'; \
echo '[smartfan-init] 提示: 飞牛 GUI 如果显示「启动失败」请不要删容器, 等 1~2 分钟刷新容器列表, 健康检查通过后自动变为运行中.'; \
exec /app/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8780 --log-level info \
|| { echo '[smartfan-init] ERROR: uvicorn 启动异常退出, 10 秒后容器结束(方便查看错误)'; sleep 10; exit 3; }"]
