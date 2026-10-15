@echo off
REM ============================================================================
REM  One-click local build of the customized RustDesk client.
REM
REM  Prerequisites - run these once first (see README.md "本地构建" section):
REM    check-env.bat                verify nothing is missing
REM    install-vcpkg-deps.bat       C++ dependencies (slow, once)
REM    apply-rustdesk-engine.bat    Flutter + patched engine (once)
REM
REM  Source:  <repo>\              (vendored upstream sits at the repo root)
REM  Output:  <repo>\flutter\build\windows\x64\runner\Release
REM
REM  Works from anywhere - every path is derived from %~dp0, so the repo can
REM  live on any drive. No hardcoded D:\Git anywhere.
REM ============================================================================
setlocal

REM --- Tool locations -------------------------------------------------------
REM Defaults match where things actually are on this machine. Override from
REM outside without editing this file:
REM     set FLUTTER_ROOT=D:\flutter && build-local.bat
REM Flutter SDK lives on C: (already installed, ~1 GB).
REM vcpkg goes to D: on purpose - its build tree is several GB and C: is tight.
if not defined FLUTTER_ROOT set FLUTTER_ROOT=c:\flutter
if not defined VCPKG_ROOT  set VCPKG_ROOT=d:\vcpkg
set PATH=%FLUTTER_ROOT%\bin;%USERPROFILE%\.cargo\bin;%PATH%
set VCPKG_DEFAULT_HOST_TRIPLET=x64-windows-static

echo Flutter:  %FLUTTER_ROOT%
echo vcpkg:    %VCPKG_ROOT%

REM 上游源码已扁平化到仓库根：%~dp0 = <repo>\build\  ->  .. = <repo>\
cd /d "%~dp0.."
if errorlevel 1 (
    echo Cannot find repo root: %~dp0..
    exit /b 1
)
if not exist ".\build.py" (
    echo ERROR: build.py not found in %CD%
    echo The vendored upstream source should sit at the repo root,
    echo alongside build\ and server\.
    exit /b 1
)

echo.
echo === Pre-flight checks ===
where cargo >nul 2>&1
if errorlevel 1 (
    echo ERROR: cargo not found in PATH.
    echo Expected at: %USERPROFILE%\.cargo\bin
    exit /b 1
)
where flutter >nul 2>&1
if errorlevel 1 (
    echo ERROR: flutter not found in PATH.
    echo Expected at: %FLUTTER_ROOT%\bin
    exit /b 1
)
if not exist "%VCPKG_ROOT%\installed\x64-windows-static" (
    echo ERROR: vcpkg dependencies not built.
    echo Run install-vcpkg-deps.bat first.
    exit /b 1
)

REM --- ffmpeg / hardware codec -------------------------------------------
REM --hwcodec makes the build use NVENC / QSV / AMF instead of software VP9.
REM It needs the ffmpeg headers that install-vcpkg-deps.bat produces.
REM Check here rather than letting cargo fail 20 minutes in with a wall of
REM C1083 "Cannot open include file: 'libavcodec/avcodec.h'".
REM Set NO_HWCODEC=1 to build software-only (faster, higher CPU when running).
if "%NO_HWCODEC%"=="1" goto :ffmpeg_checked
if exist "%VCPKG_ROOT%\installed\x64-windows-static\include\libavcodec\avcodec.h" goto :ffmpeg_checked
echo.
echo ERROR: ffmpeg headers not found in
echo     %VCPKG_ROOT%\installed\x64-windows-static\include
echo A --hwcodec build will fail with C1083 on libavcodec/avcodec.h.
echo Fix:  run install-vcpkg-deps.bat   ^(without NO_HWCODEC set^)
echo   or: set NO_HWCODEC=1 to build with software codecs only.
exit /b 1
:ffmpeg_checked

REM --- flutter-rust-bridge 胶水代码 ---------------------------------------
REM src/bridge_generated.rs / flutter/lib/generated_bridge.dart 是代码生成器
REM 产物，被 .gitignore 排除，仓库里没有。缺了它们 cargo 会直接报
REM "E0583: file not found for module bridge_generated"。
REM 首次执行会自动 cargo install codegen（约 5-15 分钟）。
echo.
echo === Checking flutter-rust-bridge artifacts ===
python "%~dp0generate-bridge.py" --check
if errorlevel 1 (
    echo Artifacts missing - generating now. First run takes 5-15 min.
    python "%~dp0generate-bridge.py"
    if errorlevel 1 (
        echo.
        echo ERROR: failed to generate bridge artifacts. See output above.
        exit /b 1
    )
)

echo Source:  %CD%
echo Rust toolchain:
cargo --version
echo Flutter:
flutter --version
echo.

echo ============================================================
echo  Starting build - expect 20 to 40 minutes.
echo  First build compiles every dependency from scratch.
echo ============================================================
echo.

set BUILD_EXTRA=--hwcodec --vram
if "%NO_HWCODEC%"=="1" set BUILD_EXTRA=--vram
echo Codec args: %BUILD_EXTRA%
python .\build.py --portable --flutter --skip-portable-pack %BUILD_EXTRA%

if errorlevel 1 (
    echo.
    echo ============================================================
    echo  BUILD FAILED
    echo.
    echo  Checklist for the usual culprits:
    echo   1. MSVC not installed - need VS Build Tools with
    echo      "Desktop development with C++" workload. THIS IS THE
    echo      MOST COMMON CAUSE. Requires admin to install.
    echo   2. vcpkg deps missing      -^> install-vcpkg-deps.bat
    echo   3. Flutter engine not swapped -^> apply-rustdesk-engine.bat
    echo   4. Wrong Rust version      -^> rustup default 1.75
    echo.
    echo  Read the actual error above; it is usually specific.
    echo ============================================================
    exit /b 1
)

echo.
echo ============================================================
if exist ".\flutter\build\windows\x64\runner\Release" (
    echo  BUILD SUCCEEDED
    echo.
    echo  Output folder:
    echo     %CD%\flutter\build\windows\x64\runner\Release
    echo.
    echo  Your server address and public key are baked into the binary,
    echo  so users do not have to configure anything.
) else (
    echo  Build exited 0 but Release folder was not found.
    echo  Inspect the output above for where it actually landed.
)
echo ============================================================
endlocal
pause
