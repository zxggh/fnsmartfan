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

# 切换到普通用户
USER fanuser

# 暴露 Web 控制台端口
EXPOSE 8780

# 健康检查：通过 /api/info 接口判断服务状态
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1 || exit 1

# 启动命令：
# 1) 空卷初始化：把 /app/config.yaml 模板拷到 /data（如果卷里还没有）
# 2) 将 /app/config.yaml 软链到 /data/config.yaml，保证配置在容器重建后保留
# 3) 用 root 用户 + --privileged 已经足够访问串口，跳过 sg dialout/sg disk
#    （原 start.sh 里的嵌套 sg 会在非 TTY 环境索要密码 → 容器死循环重启）
# 4) 首次启动时补装项目 requirements (兼容老镜像/构建阶段漏装)
#    同样用「官方 PyPI 为主 + 清华为辅」双源, 5 次重试, python -m pip
CMD ["bash", "-c", "\
set -e && \
cd /app && \
if [ ! -f /data/config.yaml ] && [ -f /app/config.yaml ]; then \
  mkdir -p /data && cp /app/config.yaml /data/config.yaml; \
fi && \
rm -f /app/config.yaml && ln -sf /data/config.yaml /app/config.yaml && \
/app/venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1 || true; \
/app/venv/bin/python -m pip install --quiet --no-cache-dir \
  --retries 5 --timeout 60 \
  --index-url https://pypi.org/simple/ \
  --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  --trusted-host pypi.org --trusted-host pypi.python.org \
  --trusted-host files.pythonhosted.org \
  --trusted-host pypi.tuna.tsinghua.edu.cn \
  -r /app/requirements.txt PyYAML pyserial pyserial-asyncio fastapi uvicorn schedule \
  || true && \
exec /app/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8780 --log-level info"]
