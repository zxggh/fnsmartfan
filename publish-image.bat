@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ============================================================
REM SmartFan 智能风扇控制器 - 一键发布到远程镜像仓库
REM 支持：
REM   1. GitHub Container Registry (ghcr.io)  ← 默认推荐（用 GitHub 账号即可）
REM   2. Docker Hub (docker.io)
REM
REM 使用前准备：
REM   - Windows 已安装并启动 Docker Desktop
REM   - 已准备好以下凭据（脚本会提示你填）：
REM     · GHCR：GitHub 用户名 + Personal Access Token (PAT, 权限勾 write:packages)
REM     · Docker Hub：Docker Hub 用户名 + Access Token / 密码
REM
REM 步骤：
REM   1) 本地构建镜像 smartfan:latest
REM   2) 打标签（ghcr.io/用户名/smartfan:latest + 可选日期标签）
REM   3) 登录远程仓库
REM   4) Push
REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo   SmartFan - 发布镜像到远程仓库
echo ============================================================
echo.

REM ---------- 1. 检查 Docker ----------
echo [0/6] 检查 Docker Desktop ...
docker version >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ Docker Desktop 未启动，请先启动后再运行本脚本
    pause
    exit /b 1
)
echo     ✅ Docker 已就绪
echo.

REM ---------- 2. 选择仓库 ----------
echo ============================================================
echo   请选择要发布的仓库：
echo   [1] GitHub GHCR  (ghcr.io / 推荐，用 GitHub 账户 PAT)
echo   [2] Docker Hub   (docker.io / 传统 Docker Hub 账户)
echo ============================================================
set /p REG_CHOICE="输入选择 (默认 1): "
if "%REG_CHOICE%"=="" set REG_CHOICE=1

if "%REG_CHOICE%"=="2" (
    set REGISTRY=docker.io
    set REG_NAME=Docker Hub
) else (
    set REGISTRY=ghcr.io
    set REG_NAME=GitHub GHCR
)

echo.
set /p USERNAME="请输入 %REG_NAME% 用户名（小写）: "
if "%USERNAME%"=="" (
    echo ❌ 用户名不能为空
    pause
    exit /b 1
)
REM 用户名转小写
for /f "usebackq delims=" %%I in (`powershell -Command "$input='%USERNAME%'; $input.ToLower()"`) do set "USERNAME=%%I"

echo.
REM 密码输入（隐藏）
<nul set /p ="请输入 Access Token / PAT / 密码（不显示）："
powershell -Command "$p=Read-Host -AsSecureString; $b=[System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($p); [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($b) | Out-File -FilePath '%TEMP%\smartfan_token.txt' -Encoding utf8"
set /p TOKEN=<"%TEMP%\smartfan_token.txt"
del "%TEMP%\smartfan_token.txt" >nul 2>nul

if "%TOKEN%"=="" (
    echo.
    echo ❌ Token 不能为空
    pause
    exit /b 1
)

REM ---------- 3. 生成标签 ----------
echo.
echo [1/6] 生成镜像标签...
set /p VERSION_TAG="请输入版本号标签 (默认 latest，也可填 v1.0.0/20260826 等): "
if "%VERSION_TAG%"=="" set VERSION_TAG=latest

set REMOTE_IMAGE_LATEST=%REGISTRY%/%USERNAME%/smartfan:latest
set REMOTE_IMAGE_VERSION=%REGISTRY%/%USERNAME%/smartfan:%VERSION_TAG%
echo     latest 标签: %REMOTE_IMAGE_LATEST%
echo     版本  标签: %REMOTE_IMAGE_VERSION%
echo.

REM ---------- 4. 本地构建 ----------
echo [2/6] 本地构建镜像 smartfan:latest ...
docker build -f Dockerfile -t smartfan:latest .
if %errorlevel% neq 0 (
    echo ❌ 构建失败，请检查日志
    pause
    exit /b 1
)
echo     ✅ 构建完成
echo.

REM ---------- 5. 打远程标签 ----------
echo [3/6] 打远程标签 ...
docker tag smartfan:latest "%REMOTE_IMAGE_LATEST%"
docker tag smartfan:latest "%REMOTE_IMAGE_VERSION%"
echo     ✅ 标签完成
echo.

REM ---------- 6. 登录仓库 ----------
echo [4/6] 登录 %REG_NAME% ...
echo %TOKEN%| docker login "%REGISTRY%" -u "%USERNAME%" --password-stdin
if %errorlevel% neq 0 (
    echo ❌ 登录失败，检查用户名 / Token 是否正确
    echo    GHCR 的 Token 必须勾 write:packages 权限
    echo    Docker Hub 的 Access Token 必须勾 read/write/delete 权限
    pause
    exit /b 1
)
echo     ✅ 登录成功
echo.

REM ---------- 7. 推送 ----------
echo [5/6] 推送镜像到 %REG_NAME% ...
echo     → 推送 %REMOTE_IMAGE_VERSION% ...
docker push "%REMOTE_IMAGE_VERSION%"
if %errorlevel% neq 0 (
    echo ❌ 推送版本标签失败
    pause
    exit /b 1
)
echo     → 推送 %REMOTE_IMAGE_LATEST% ...
docker push "%REMOTE_IMAGE_LATEST%"
if %errorlevel% neq 0 (
    echo ❌ 推送 latest 失败
    pause
    exit /b 1
)
echo     ✅ 推送完成
echo.

REM ---------- 8. 登出（可选安全） ----------
echo [6/6] 登出（本地不保存凭据可选） ...
docker logout "%REGISTRY%" >nul 2>nul

REM ---------- 完成信息 ----------
echo.
echo ============================================================
echo   ✅ 发布成功！
echo ============================================================
echo.
echo   镜像地址：
echo     %REMOTE_IMAGE_LATEST%
echo     %REMOTE_IMAGE_VERSION%
echo.
echo   下次更新流程（5 秒）：
echo     1. Windows 双击本脚本，新版本会覆盖 latest
echo     2. NAS 界面 → 容器 → smartfan → 「重新拉取 / Pull latest」→ 点重启
echo        或容器列表右键 → 「重新创建」
echo.
echo   NAS 手动拉取（若 compose 拉取失败/401）：
echo     容器 → 镜像 → 拉取镜像 → 自定义仓库地址填写：
echo       %REMOTE_IMAGE_LATEST%
echo.
pause
endlocal
