@echo off
REM ============================================================================
REM 本地构建 wedesk 服务端 Docker 镜像（容器内从源码编译）
REM
REM 这是 Windows / macOS 上得到 Linux 服务端镜像最简单的路子：
REM 本机不需要 Rust、不需要 musl、不需要 MSVC —— 编译全发生在容器里。
REM
REM 用法：
REM   build-server-image.bat                 构建 wedesk-server:latest（linux/amd64）
REM   build-server-image.bat 1.1.17          指定标签
REM   build-server-image.bat --platform linux/arm64 1.1.17
REM
REM 耗时说明：首次构建约 15-25 分钟（release profile 开了 LTO + codegen-units=1）。
REM 之后再构建走 Docker 层缓存与 cache mount，只重编改动的部分。
REM
REM 构建上下文必须是 server\ 目录，因为它下面才放着 rustdesk-server\ 源码
REM 和排除私钥用的 .dockerignore。
REM ============================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0.."
set "SERVER_DIR=%CD%\server"

echo.
echo ==== 构建 wedesk 服务端镜像 ====
echo.

REM ---- 参数解析 ----
set "TAG=latest"
set "PLATFORM=linux/amd64"
set "EXPECT_PLATFORM=0"
for %%a in (%*) do (
    if "%%a"=="--platform" (
        set "EXPECT_PLATFORM=1"
    ) else (
        if "!EXPECT_PLATFORM!"=="1" (
            set "PLATFORM=%%a"
            set "EXPECT_PLATFORM=0"
        ) else (
            set "TAG=%%a"
        )
    )
)

echo 上下文:   %SERVER_DIR%
echo 平台:     %PLATFORM%
echo 镜像标签: wedesk-server:%TAG%
echo.

REM ---- 1. docker 是否可用 ----
where docker >nul 2>&1
if errorlevel 1 (
    echo [x] 找不到 docker。请安装 Docker Desktop 并确保已启动。
    exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
    echo [x] Docker daemon 未运行。请先启动 Docker Desktop。
    exit /b 1
)

REM ---- 2. 源码与 .dockerignore 校验 ----
if not exist "%SERVER_DIR%\rustdesk-server\Cargo.toml" (
    echo [x] 找不到服务端源码: %SERVER_DIR%\rustdesk-server\Cargo.toml
    exit /b 1
)
if not exist "%SERVER_DIR%\.dockerignore" (
    echo [!] 缺少 .dockerignore —— 强烈建议保留。
    echo     它负责把 server\data\ 排除在构建上下文之外，
    echo     否则私钥 id_ed25519 会被烧进镜像层。
)

REM ---- 3. 提醒 data 目录风险 ----
if exist "%SERVER_DIR%\data\id_ed25519" (
    echo [i] 检测到 server\data\id_ed25519（服务器私钥）。
    echo     .dockerignore 已将其排除，不会进入镜像。
)

REM ---- 4. 构建 ----
cd /d "%SERVER_DIR%"
echo.
echo ---- docker build（容器内编译 musl 静态二进制，首次较慢）----
echo.
docker build --platform %PLATFORM% -t wedesk-server:%TAG% -f Dockerfile .
if errorlevel 1 (
    echo.
    echo [x] 构建失败。常见原因：
    echo     1. 容器内下载 crates.io 依赖超时 —— 重试一次通常就好
    echo     2. 磁盘空间不足 —— 编译中间产物可达数 GB
    echo     3. cross-platform 编译未开 QEMU —— 换成 linux/amd64 再试
    exit /b 1
)

echo.
echo ==== 构建完成 ====
echo.
docker images wedesk-server:%TAG%
echo.
echo 启动服务：
echo     cd /d server
echo     copy .env.example .env
echo     编辑 .env 填写 RELAY_HOST=^<服务器IP^>
echo     docker compose up -d
echo.
echo 如果没要用 Docker，也可以直接用源码编译 Windows 版：
echo     build\build-server.bat
