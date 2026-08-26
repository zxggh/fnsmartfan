@echo off
chcp 65001 >nul
setlocal

echo ============================================================
echo   SmartFan 智能风扇控制器 - Windows 本地构建并导出离线镜像
echo ============================================================
echo.

cd /d "%~dp0"

REM ============================================================
REM 1. 检查 Docker Desktop 是否启动
REM ============================================================
echo [1/3] 检查 Docker Desktop 是否运行...
docker version >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ❌ 错误：Docker Desktop 未启动或未安装！
    echo.
    echo 请先：
    echo    1. 安装 Docker Desktop（官网 https://www.docker.com/products/docker-desktop/）
    echo    2. 启动 Docker Desktop，等右下角鲸鱼图标稳定
    echo    3. 再重新双击运行本脚本
    echo.
    pause
    exit /b 1
)
echo     ✅ Docker 已就绪
echo.

REM ============================================================
REM 2. 构建镜像
REM ============================================================
echo [2/3] 开始构建镜像 smartfan:latest ...
echo.

docker build -f Dockerfile -t smartfan:latest .
if %errorlevel% neq 0 (
    echo.
    echo ❌ 构建失败！请检查上方报错信息。
    echo.
    echo 常见问题：
    echo   - 拉不动 python:3.10-slim？
    echo     Docker Desktop - Settings - Docker Engine 加国内镜像加速：
    echo       "registry-mirrors": ["https://registry.cn-hangzhou.aliyuncs.com"]
    echo     Apply & Restart 后再重试
    echo.
    pause
    exit /b 1
)
echo.
echo     ✅ 镜像构建完成
echo.

REM ============================================================
REM 3. 导出为 tar
REM ============================================================
echo [3/3] 导出镜像为 smartfan-image.tar （约 300~400MB，请稍等）...
echo.

docker save -o "smartfan-image.tar" smartfan:latest
if %errorlevel% neq 0 (
    echo.
    echo ❌ 导出失败！请检查磁盘空间或权限。
    pause
    exit /b 1
)
echo.
echo ============================================================
echo   ✅ 全部完成！
echo ============================================================
echo.
echo   导出文件：%~dp0smartfan-image.tar
echo.
echo   下一步：
echo     1. 把这个 tar 上传到 NAS
echo     2. NAS 容器管理 - 镜像 - 添加镜像 - 从NAS文件添加
echo     3. 选这个 tar，导入完成后用 docker-compose.yml 创建项目
echo.
pause
endlocal
