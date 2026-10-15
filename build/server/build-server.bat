@echo off
REM ============================================================================
REM 本机编译服务端 hbbs / hbbr（Windows 原生 exe）
REM
REM 服务端源码就在本仓库 server/src/，是独立 Cargo workspace，
REM 不依赖任何子模块、不需要联网拉 submodule —— 但首次编译要下载依赖。
REM
REM 产物：server\bin\hbbs.exe / hbbr.exe / rustdesk-utils.exe
REM
REM 用法：
REM   build-server.bat              编译 release 版本
REM   build-server.bat --debug      编译 debug 版本（排障用，启动快、体积小一半以上）
REM   build-server.bat --clean      先清理 target 再编译
REM
REM 关于 Rust 版本 —— 这里最容易踩坑，务必看清：
REM   客户端锁定 Rust 1.75（sciter/bridge 的 i128 ABI 要求），
REM   但服务端 Cargo.lock 是 lockfile v4，cargo < 1.78 根本读不了，
REM   会报：lock file version 4 requires `-Znext-lockfile-bump`
REM   所以本脚本强制切到 stable toolchain。两个 workspace 各用各的，互不影响。
REM ============================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0..\.."
set "SRV=%CD%\server\src"
set "OUT=%CD%\server\bin"

echo.
echo ==== Wedesk 服务端本地编译 ====
echo.
echo 源码目录: %SRV%
echo 产物目录: %OUT%
echo.

REM ---- 0. 参数 ----
set "PROFILE=--release"
set "CLEAN=0"
if /i "%~1"=="--debug" set "PROFILE="
if /i "%~1"=="--clean" set "CLEAN=1"
if /i "%~2"=="--debug" set "PROFILE="
if /i "%~2"=="--clean" set "CLEAN=1"

REM ---- 1. 源码是否就位 ----
if not exist "%SRV%\Cargo.toml" (
    echo [x] 找不到服务端源码: %SRV%\Cargo.toml
    echo     本目录应是 rustdesk-server 的 vendored 副本。
    echo     恢复方式: python build\server\vendor-server.py
    exit /b 1
)
if not exist "%SRV%\libs\hbb_common\src\lib.rs" (
    echo [x] 服务端依赖的 hbb_common 是空目录。
    echo     上游把它作为 submodule，本仓库已 vendor 化，不该是空的。
    echo     恢复方式: python build\server\vendor-server.py
    exit /b 1
)

REM ---- 2. Rust 工具链 ----
where cargo >nul 2>&1
if errorlevel 1 (
    echo [x] 找不到 cargo。请先安装 Rust: https://rustup.rs
    exit /b 1
)

REM 服务端必须用 >= 1.78 的 toolchain。
REM rustup toolchain list 的输出形如 "1.75-x86_64-pc-windows-msvc (active, default)"；
REM 这里不逐情解析版本，而是在下面尝试 +stable.bat 调用，让 rustup 自己判断。
where rustup >nul 2>&1
if errorlevel 1 (
    echo [!] 未检测到 rustup，将使用当前 cargo。
    set "TOOLCHAIN_PREFIX="
) else (
    echo ---- 检查 stable toolchain ----
    rustup toolchain list | findstr /r /c:"^stable" >nul 2>&1
    if errorlevel 1 (
        echo     未安装 stable toolchain。服务端 lockfile v4 需要 cargo ^>= 1.78，
        echo     正在安装 stable（仅这次，约 200MB，取决于网络）...
        rustup toolchain install stable --profile minimal --no-self-update
        if errorlevel 1 (
            echo [x] stable toolchain 安装失败。请手动执行后再跑本脚本：
            echo     rustup toolchain install stable
            exit /b 1
        )
    )
    REM RUSTUP_TOOLCHAIN 优先级高于目录里的 rust-toolchain 文件，
    REM 保证服务端一定用 stable，不受仓库任何版本固定影响。
    set "RUSTUP_TOOLCHAIN=stable"
    echo     使用 toolchain: stable
)

REM ---- 3. MSVC 链接器 ----
REM Windows 目标需要 MSVC；用 GNU toolchain 会在 link.exe 处失败。
where cl >nul 2>&1
if errorlevel 1 (
    echo [!] 未检测到 MSVC 编译器（cl.exe）。
    echo     服务端是纯 Rust + C 依赖（libsodium/sqlite），必须有 MSVC。
    echo     若在普通 cmd 下跑本脚本，请先执行 一次 vsdevcmd 或
    echo     "x64 Native Tools Command Prompt for VS"。
    echo     也可以用 Docker 构建，完全不需要本机工具链：
    echo         cd server ^&^& docker build -t wedesk-server:latest -f deploy\Dockerfile .
    exit /b 1
)

cd /d "%SRV%"

REM ---- 4. sqlx 编译期校验 ----
REM hbbs 的 SQL 查询宏在编译阶段要连一个真实 SQLite 库来校验 SQL，
REM 上游为此在仓库根放了 db_v2.sqlite3。不设 DATABASE_URL 会直接编译失败。
set "DBPOSIX=%SRV:\=/%"
set "DATABASE_URL=sqlite:%DBPOSIX%/db_v2.sqlite3"
echo ---- DATABASE_URL=%DATABASE_URL%

if "%CLEAN%"=="1" (
    echo ---- 清理 target ----
    cargo clean
)

echo.
echo ---- 开始编译（首次全量约 5-15 分钟，LTO 已开会比较慢）----
echo.
cargo build --locked %PROFILE% --bin hbbs --bin hbbr --bin rustdesk-utils
if errorlevel 1 (
    echo.
    echo [x] 编译失败。常见原因：
    echo     1. Rust 版本过旧 —— 服务端需要 ^>= 1.78，见本脚本顶部说明
    echo     2. DATABASE_URL 未生效 —— 需 sqlite:// 形式的绝对路径
    echo     3. 依赖下载失败 —— 检查网络或代理
    exit /b 1
)

REM ---- 5. 收集产物 ----
set "TARGETDIR=%SRV%\target\release"
if defined PROFILE ( set "TARGETDIR=%SRV%\target\release" ) else ( set "TARGETDIR=%SRV%\target\debug" )

if not exist "%OUT%" mkdir "%OUT%"
copy /y "%TARGETDIR%\hbbs.exe"           "%OUT%\" >nul
copy /y "%TARGETDIR%\hbbr.exe"           "%OUT%\" >nul
copy /y "%TARGETDIR%\rustdesk-utils.exe" "%OUT%\" >nul

echo.
echo ==== 编译完成 ====
echo.
dir "%OUT%"
echo.
echo 运行方式（两个进程要同时起）：
echo     cd /d server\bin
echo     hbbr.exe
echo     hbbs.exe -p 5187 -r ^<本机IP^>:5188
echo.
echo 首次运行会在当前目录生成 id_ed25519 / id_ed25519.pub，
echo 公钥内容要填进客户端叠置服务器配置（或直接烧进 libs/hbb_common 的 RS_PUB_KEY）。
echo 详细信息: docs\09-server-build.md
