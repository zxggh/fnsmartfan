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

# 安装基础依赖：base64解码工具、tar、sudo等
# 配置 apt 使用国内源加速（阿里云 Debian 源）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
 || sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list 2>/dev/null \
 || true \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
    coreutils \
    tar \
    sudo \
    curl \
 && rm -rf /var/lib/apt/lists/*

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

# 安装 Python 依赖（使用国内清华 PyPI 镜像源加速）
RUN /app/venv/bin/pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    pyserial-asyncio fastapi uvicorn

# 确保启动脚本有执行权限
RUN chmod +x /app/start.sh 2>/dev/null || true

# 创建非 root 用户运行服务（安全最佳实践）
RUN groupadd -r dialout 2>/dev/null || true \
 && useradd -r -u 1000 -G dialout fanuser \
 && chown -R fanuser:fanuser /app

# 切换到普通用户
USER fanuser

# 暴露 Web 控制台端口
EXPOSE 8780

# 健康检查：通过 /api/info 接口判断服务状态
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -s -m 3 http://127.0.0.1:8780/api/info >/dev/null 2>&1 || exit 1

# 启动命令：使用虚拟环境中的 Python 运行 start.sh
CMD ["bash", "-c", "cd /app && ./start.sh"]
