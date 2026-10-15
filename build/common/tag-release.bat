@echo off
REM ============================================================================
REM 打版本标签并推送 —— 推送后 GitHub Actions 自动构建并发布 Release
REM
REM 版本号取自 Cargo.toml，不手工输入。
REM CI 里有强校验：标签版本必须等于 Cargo.toml 版本，不一致直接失败。
REM 与其等 40 分钟构建跑完才被告知版本号错了，不如在这里就取准。
REM
REM 用法：
REM   tag-release.bat          打 v<Cargo.toml 版本> 并推送
REM   tag-release.bat --dry    只显示将要做什么，不实际操作
REM ============================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0..\.."

set "DRY=0"
if /i "%~1"=="--dry"     set "DRY=1"
if /i "%~1"=="--dry-run" set "DRY=1"

echo.
echo ==== Wedesk 版本发布 ====
echo.

REM ---- 0. 定位 python（优先 python，回退 py -3） ----
set "PY=python"
where python >nul 2>&1
if errorlevel 1 (
    where python3 >nul 2>&1
    if errorlevel 1 ( set "PY=py -3" ) else ( set "PY=python3" )
)

REM ---- 1. 读取版本号 ----
REM 版本提取放在 get-version.py 里：cmd 的 for /f 解析不了命令行中的括号，
REM 内联 python -c 会在 .read() 处报 "was unexpected at this time"。
set "VERSION="
for /f "usebackq delims=" %%v in (`%PY% "%~dp0get-version.py" client\Cargo.toml 2^>nul`) do set "VERSION=%%v"
if not defined VERSION (
    echo [x] 无法从 client\Cargo.toml 读取版本号。
    echo     请确认已安装 python 且可在命令行直接调用。
    exit /b 1
)
set "TAG=v%VERSION%"
echo client\Cargo.toml 版本: %VERSION%
echo 将要打的标签:    %TAG%
echo.

REM ---- 2. 工作区必须干净 ----
git diff --quiet HEAD 2>nul
if errorlevel 1 (
    echo [x] 工作区有未提交的改动，请先提交或暂存。
    git status --short
    exit /b 1
)
echo [ok] 工作区干净

REM ---- 3. 与远程 main 对齐 ----
git fetch origin main >nul 2>&1
set "LOCAL="
set "REMOTE="
for /f "usebackq delims=" %%h in (`git rev-parse HEAD 2^>nul`) do set "LOCAL=%%h"
for /f "usebackq delims=" %%h in (`git rev-parse origin/main 2^>nul`) do set "REMOTE=%%h"
if defined REMOTE (
    if not "%LOCAL%"=="%REMOTE%" (
        echo [x] 本地 HEAD 与 origin/main 不一致 —— 发布的提交必须是远程 main 上的。
        echo     本地:  %LOCAL%
        echo     远程:  %REMOTE%
        echo     请先 git pull / git push 对齐后再发版。
        exit /b 1
    )
    echo [ok] 与 origin/main 一致
) else (
    echo [!] 取不到 origin/main，跳过对齐检查
)

REM ---- 4. 标签不能重复 ----
git rev-parse -q --verify "refs/tags/%TAG%" >nul 2>&1
if not errorlevel 1 (
    echo [x] 标签 %TAG% 已存在。
    echo     版本号没变但代码有更新时：先改 client\Cargo.toml 的 version 再发，
    echo     或删掉旧标签重发（已下载旧版的人不会自动更新，慎删）。
    exit /b 1
)
echo [ok] 标签 %TAG% 可用
echo.

REM ---- 5. 确认 ----
echo 将要执行：
echo     git tag -a %TAG% -m "release %TAG%"
echo     git push origin %TAG%
echo.
echo 推送后 GitHub Actions 会自动构建并发布 Release（约 40-90 分钟）。
echo.
set "CONFIRM="
set /p CONFIRM=确认发布？输入 y 继续，其它任意键取消: 
if /i not "%CONFIRM%"=="y" (
    echo 已取消。
    exit /b 0
)

if "%DRY%"=="1" (
    echo.
    echo [dry-run] 到此为止，未做任何改动。
    exit /b 0
)

REM ---- 6. 打标签并推送 ----
git tag -a "%TAG%" -m "release %TAG%"
if errorlevel 1 (
    echo [x] 打标签失败。
    exit /b 1
)

git push origin "%TAG%"
if errorlevel 1 (
    echo [x] 推送失败。标签已在本地创建，稍后可重试：
    echo         git push origin %TAG%
    exit /b 1
)

echo.
echo [ok] 已推送 %TAG%
for /f "usebackq delims=" %%r in (`git config --get remote.origin.url 2^>nul`) do set "RURL=%%r"
if defined RURL (
    set "RURL=%RURL:git@github.com:=https://github.com/%"
    set "RURL=%RURL:.git=%"
    echo     构建与发布进度: %RURL%/actions
    echo     Release 页面:   %RURL%/releases
)
endlocal & exit /b 0
