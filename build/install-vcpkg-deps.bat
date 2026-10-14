@echo off
REM ============================================================================
REM  Install vcpkg and build the C++ dependencies RustDesk needs.
REM
REM  This is the SLOWEST step (30-120 min): it compiles libvpx/aom from source.
REM  You only need to run it once - the artifacts stay in C:\vcpkg and get
REM  reused by every later build.
REM
REM  PREREQUISITE: Visual Studio Build Tools with "Desktop development with C++"
REM ============================================================================
setlocal

set VCPKG_ROOT=C:\vcpkg
set VCPKG_COMMIT=9e593bb18ea69cc5095e012465dcd675a822ed0d
set TRIPLET=x64-windows-static

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

cd /d "%VCPKG_ROOT%"

echo.
echo === [2/4] Pinning to the commit the official CI uses ===
git checkout %VCPKG_COMMIT%
if errorlevel 1 echo WARNING: could not pin version, using current checkout.

echo.
echo === [3/4] Bootstrapping vcpkg ===
if not exist "%VCPKG_ROOT%\vcpkg.exe" (
    call bootstrap-vcpkg.bat
    if errorlevel 1 echo WARNING: bootstrap returned an error.
)

echo.
echo === [4/4] Building dependencies - THIS TAKES A LONG TIME ===
echo Building: libvpx libyuv opus aom
echo Go grab a coffee. Logs land in %VCPKG_ROOT%\buildtrees\ if it fails.
echo.
set VCPKG_DEFAULT_HOST_TRIPLET=%TRIPLET%
"%VCPKG_ROOT%\vcpkg.exe" install libvpx:%TRIPLET% libyuv:%TRIPLET% opus:%TRIPLET% aom:%TRIPLET% --x-install-root="%VCPKG_ROOT%\installed"

if errorlevel 1 (
    echo.
    echo FAILED to build dependencies.
    echo Most common cause: MSVC not installed.
    echo Install Visual Studio Build Tools and tick "Desktop development with C++".
    exit /b 1
)

echo.
echo ============================================================
echo  Dependencies ready.
echo  Persisting VCPKG_ROOT for future shells...
echo ============================================================
setx VCPKG_ROOT "%VCPKG_ROOT%" >nul
echo Done. VCPKG_ROOT=%VCPKG_ROOT%
endlocal
