@echo off
REM ============================================================================
REM  Install vcpkg and build the C++ dependencies RustDesk needs.
REM
REM  MANIFEST MODE: this reads vcpkg.json under client\ instead of listing
REM  packages on the command line. That distinction is the whole point ---
REM  vcpkg.json is what pulls in ffmpeg + mfx-dispatch and wires up the
REM  overlay ports in ./res/vcpkg (the upstream ffmpeg port carries patches
REM  the public one does not have).
REM
REM  Listing "libvpx libyuv opus aom" by hand silently omits ffmpeg, and the
REM  failure only surfaces much later, deep in the cargo log:
REM      mfx_encode.cpp(3): fatal error C1083:
REM      Cannot open include file: 'libavcodec/avcodec.h'
REM  The error is two steps removed from its cause, which is why this file
REM  now just defers to the manifest.
REM
REM  ffmpeg is compiled FROM SOURCE and is by far the slowest package.
REM  Budget 60-150 min on a first run; later runs hit the binary cache.
REM  Set NO_HWCODEC=1 to skip ffmpeg (software codecs only, much faster).
REM
REM  Default location is D: on purpose: the vcpkg build tree runs to several GB
REM  and C: is short on space. Override with:  set VCPKG_ROOT=X:\somewhere
REM  (keep it in sync with build-local.bat, which defaults to the same place)
REM
REM  PREREQUISITE: Visual Studio Build Tools with "Desktop development with C++"
REM ============================================================================
setlocal

if not defined VCPKG_ROOT set VCPKG_ROOT=D:\vcpkg
set VCPKG_COMMIT=9e593bb18ea69cc5095e012465dcd675a822ed0d
set TRIPLET=x64-windows-static

REM --- host triplet -------------------------------------------------------
REM vcpkg.json declares ffmpeg as host:true with platform `... & static`.
REM The host triplet defaults to x64-windows (dynamic CRT), which does NOT
REM satisfy `static` --- so vcpkg silently skips ffmpeg and reports success.
REM Setting this makes host == target and ffmpeg actually gets built.
if "%NO_HWCODEC%"=="1" (
    set HOST_TRIPLET=x64-windows
) else (
    set HOST_TRIPLET=%TRIPLET%
)
set VCPKG_DEFAULT_HOST_TRIPLET=%HOST_TRIPLET%

echo vcpkg root:     %VCPKG_ROOT%
echo target triplet: %TRIPLET%
echo host triplet:   %HOST_TRIPLET%
if "%NO_HWCODEC%"=="1" echo hwcodec:        DISABLED - ffmpeg skipped, fast build
if not "%NO_HWCODEC%"=="1" echo hwcodec:        ENABLED - ffmpeg compiled from source
echo.

REM --- client dir: manifest mode must run where vcpkg.json lives ------------
REM %~dp0 = <repo>\build\client\  ->  ..\..\client = <repo>\client
cd /d "%~dp0..\..\client"
if errorlevel 1 (
    echo FAILED: cannot enter client dir %~dp0..\..\client
    exit /b 1
)
if not exist ".\vcpkg.json" (
    echo FAILED: vcpkg.json not found in %CD%
    echo This script must live in ^<repo^>\build\client\ and be run from a
    echo repo whose vendored upstream client source is under client\.
    exit /b 1
)
echo client 目录:    %CD%
echo.

echo === [1/4] Fetching vcpkg ===
if exist "%VCPKG_ROOT%\vcpkg.exe" (
    echo vcpkg already present at %VCPKG_ROOT%, skipping clone.
) else (
    if not exist "%VCPKG_ROOT%" (
        git clone https://github.com/microsoft/vcpkg.git "%VCPKG_ROOT%"
        if errorlevel 1 (
            echo.
            echo FAILED: could not clone vcpkg.
            echo If you are behind a proxy, set it first:
            echo     set http_proxy=http://127.0.0.1:7890
            echo     set https_proxy=http://127.0.0.1:7890
            exit /b 1
        )
    )
)

echo.
echo === [2/4] Pinning to the commit the official CI uses ===
echo This must match builtin-baseline in vcpkg.json.
pushd "%VCPKG_ROOT%"
git checkout %VCPKG_COMMIT%
if errorlevel 1 echo WARNING: could not pin version, using current checkout.
popd

echo.
echo === [3/4] Bootstrapping vcpkg ===
if not exist "%VCPKG_ROOT%\vcpkg.exe" (
    pushd "%VCPKG_ROOT%"
    call bootstrap-vcpkg.bat -disableMetrics
    popd
)
if not exist "%VCPKG_ROOT%\vcpkg.exe" (
    echo FAILED: bootstrap-vcpkg did not produce vcpkg.exe.
    echo Try running bootstrap-vcpkg.bat manually in %VCPKG_ROOT%
    exit /b 1
)

echo.
echo === [4/4] Building dependencies - THIS TAKES A LONG TIME ===
echo Manifest mode: reading vcpkg.json from %CD%
echo Includes: libvpx libyuv opus aom libjpeg-turbo mfx-dispatch ffmpeg
echo Binary cache: %VCPKG_ROOT%\bincache
echo Go grab a coffee. Logs land in %VCPKG_ROOT%\buildtrees\ if it fails.
echo.
set VCPKG_BINARY_SOURCES=clear;files,%VCPKG_ROOT%\bincache,readwrite

"%VCPKG_ROOT%\vcpkg.exe" install --triplet %TRIPLET% --x-install-root="%VCPKG_ROOT%\installed"

if errorlevel 1 (
    echo.
    echo FAILED to build dependencies.
    echo Most common cause: MSVC not installed.
    echo Install Visual Studio Build Tools and tick "Desktop development with C++".
    echo.
    echo If it was ffmpeg that failed, its logs are at:
    echo     %VCPKG_ROOT%\buildtrees\ffmpeg\
    exit /b 1
)

echo.
echo === Verifying key artifacts ===
set INC=%VCPKG_ROOT%\installed\%TRIPLET%\include
set MISSING=0
if not exist "%INC%\vpx\vpx_encoder.h" echo   [MISS] libvpx headers
if not exist "%INC%\vpx\vpx_encoder.h" set MISSING=1
if not exist "%INC%\libyuv.h" echo   [MISS] libyuv headers
if not exist "%INC%\libyuv.h" set MISSING=1

if "%NO_HWCODEC%"=="1" (
    echo   [skip] ffmpeg headers - NO_HWCODEC=1, software codecs only
) else (
    if exist "%INC%\libavcodec\avcodec.h" (
        echo   [OK]   ffmpeg headers - hardware codec available
    ) else (
        echo   [MISS] ffmpeg headers - a --hwcodec build will fail with C1083
        set MISSING=1
    )
)

if "%MISSING%"=="1" (
    echo.
    echo Some headers are missing. Check %INC%
    exit /b 1
)

echo.
echo ============================================================================
echo  Dependencies ready.
echo  Persisting VCPKG_ROOT for future shells...
echo ============================================================================
setx VCPKG_ROOT "%VCPKG_ROOT%" >nul
echo Done. VCPKG_ROOT=%VCPKG_ROOT%
endlocal
