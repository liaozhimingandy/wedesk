@echo off
REM ============================================================================
REM  Install Flutter 3.24.5 and swap in the RustDesk-patched engine.
REM
REM  WHY THE ENGINE SWAP IS REQUIRED:
REM  RustDesk ships its own Flutter engine build (windows-x64-release).
REM  Compiling against the stock Flutter engine yields a client whose window
REM  behaviour is broken. The official CI performs this exact swap - skipping
REM  it is one of the most common local-build failures.
REM
REM  The URL below is taken verbatim from the official flutter-build.yml.
REM ============================================================================
setlocal

set FLUTTER_VERSION=3.24.5
if not defined FLUTTER_ROOT set FLUTTER_ROOT=c:\flutter
set FLUTTER_ZIP=%~dp0flutter_sdk.zip
echo Flutter target: %FLUTTER_ROOT%

echo.
echo === [1/4] Extracting Flutter SDK ===
if exist "%FLUTTER_ROOT%\bin\flutter.bat" (
    echo Flutter already installed at %FLUTTER_ROOT%
) else (
    if not exist "%FLUTTER_ZIP%" (
        echo.
        echo Missing SDK archive: %FLUTTER_ZIP%
        echo Download it first:
        echo   curl -L -o flutter_sdk.zip https://storage.googleapis.com/flutter_infra_release/releases/stable/windows/flutter_windows_%FLUTTER_VERSION%-stable.zip
        exit /b 1
    )
    echo Extracting... this takes several minutes (about 1 GB).
    echo Trying Windows PowerShell Expand-Archive first (most reliable)...
    powershell -NoProfile -Command "Expand-Archive -Path '%FLUTTER_ZIP%' -DestinationPath 'C:\' -Force"
    if errorlevel 1 (
        echo PowerShell method failed, falling back to bsdtar...
        tar -xf "%FLUTTER_ZIP%" -C C:\
        if errorlevel 1 (
            echo.
            echo ERROR: both extraction methods failed.
            echo Extract manually with 7-Zip / Windows Explorer to C:\flutter
            echo The archive layout already has the top-level "flutter" folder.
            exit /b 1
        )
    )
)

set PATH=%FLUTTER_ROOT%\bin;%PATH%

echo.
echo === [2/4] Downloading the RustDesk custom engine ===
cd /d "%TEMP%"
if not exist windows-x64-release.zip (
    curl -L -o windows-x64-release.zip https://github.com/rustdesk/engine/releases/download/main/windows-x64-release.zip
    if errorlevel 1 (
        echo ERROR: could not download engine.
        echo If behind a proxy:  set https_proxy=http://127.0.0.1:7890
        exit /b 1
    )
)
if not exist windows-x64-release mkdir windows-x64-release
tar -xf windows-x64-release.zip -C windows-x64-release

echo.
echo === [3/4] Replacing the engine artifacts ===
set ENGINE_DIR=%FLUTTER_ROOT%\bin\cache\artifacts\engine\windows-x64-release
if not exist "%ENGINE_DIR%" (
    echo Engine cache missing, running flutter precache...
    call flutter precache --windows
)
xcopy /Y /E /I "%TEMP%\windows-x64-release\*" "%ENGINE_DIR%\"
if errorlevel 1 (
    echo ERROR: could not copy engine files.
    exit /b 1
)
echo Engine replaced.

echo.
echo === [4/4] Applying the Flutter 3.24 dropdown patch ===
REM %~dp0 = <repo>\build\  ->  ..\.github = <repo>\.github
set FLUTTER_PATCH_SRC=%~dp0..\.github\patches\flutter_3.24.4_dropdown_menu_enableFilter.diff
if exist "%FLUTTER_PATCH_SRC%" (
    copy /Y "%FLUTTER_PATCH_SRC%" "%FLUTTER_ROOT%\" >nul
    cd /d "%FLUTTER_ROOT%"
    git apply flutter_3.24.4_dropdown_menu_enableFilter.diff
    echo Patch attempt done - if it says "already exists" that is fine.
) else (
    echo WARNING: patch file not found, skipping.
    echo Expected: %FLUTTER_PATCH_SRC%
)

echo.
echo ============================================================
echo  Flutter ready at %FLUTTER_ROOT%
echo  Add this to your permanent PATH:
echo      C:\flutter\bin
echo ============================================================
endlocal
