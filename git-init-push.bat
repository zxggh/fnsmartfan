@echo off
REM ============================================================
REM SmartFan - 一键本地 Git 初始化 + 提交 + 推送到 GitHub
REM （解决右键没有 Git Bash Here 的问题）
REM
REM 使用前请先修改下面三行：
REM   GITHUB_USER   → 你的 GitHub 用户名（小写）
REM   REPO_NAME     → 你在 GitHub 上建的仓库名
REM   COMMIT_MSG    → 这次提交的说明（随便写）
REM ============================================================

REM ======== 用户配置区（请先改这里！）========
set GITHUB_USER=zxggh
set REPO_NAME=smartfan
set COMMIT_MSG=初次提交：SmartFan 智能风扇控制器
REM ======== 配置区结束 ========

chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   SmartFan - 一键初始化 Git 并推送
echo ============================================================
echo.
echo   目标仓库: https://github.com/%GITHUB_USER%/%REPO_NAME%.git
echo   提交信息: %COMMIT_MSG%
echo   当前目录: %cd%
echo.

REM ---------- 检查 git 可用 ----------
git --version >nul 2>nul
if %errorlevel% neq 0 (
    echo ❌ 找不到 git 命令，请先安装 Git for Windows
    echo     下载地址：https://git-scm.com/download/win
    pause
    exit /b 1
)
echo [1/6] ✅ Git 已就绪

REM ---------- 检查远程仓库是否已关联 ----------
git rev-parse --git-dir >nul 2>nul
if %errorlevel% neq 0 (
    echo [2/6] ⏳ 初始化 Git 仓库 ...
    git init || goto :error
    git branch -M main || goto :error
) else (
    echo [2/6] ✅ 已有 Git 仓库，跳过初始化
)

REM ---------- 关联远程 ----------
git remote get-url origin >nul 2>nul
if %errorlevel% neq 0 (
    echo [3/6] ⏳ 关联远程仓库 ...
    git remote add origin "https://github.com/%GITHUB_USER%/%REPO_NAME%.git" || goto :error
) else (
    echo [3/6] ✅ 远程仓库已关联，跳过
)

REM ---------- add ----------
echo [4/6] ⏳ 加入文件 ...
git add . || goto :error

REM ---------- 检查有没有改动 ----------
git diff --cached --quiet >nul 2>nul
if %errorlevel% equ 0 (
    echo       （没有改动需要提交）
    goto :skip_commit
)

REM ---------- commit ----------
echo [5/6] ⏳ 提交代码 ...
git commit -m "%COMMIT_MSG%" || goto :error

:skip_commit
echo [6/6] ⏳ 推送到 GitHub（首次会弹出 GitHub 登录框）...
echo       登录方式：
echo         · 推荐「浏览器登录」按提示操作
echo         · 或者用 Token（ghp_开头，勾 repo + write:packages 权限）
git push -u origin main || goto :error

echo.
echo ============================================================
echo   ✅ 推送成功！
echo ============================================================
echo.
echo   仓库地址  : https://github.com/%GITHUB_USER%/%REPO_NAME%
echo   Actions   : https://github.com/%GITHUB_USER%/%REPO_NAME%/actions
echo   Packages  : https://github.com/%GITHUB_USER%?tab=packages
echo.
echo   👉 打开 Actions 页面，看到绿色勾就说明镜像已自动构建完成
echo.
pause
exit /b 0

:error
echo.
echo ❌ 执行失败，错误码 %errorlevel%
pause
exit /b %errorlevel%
