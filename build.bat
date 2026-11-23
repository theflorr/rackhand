@echo off
setlocal
cd /d "%~dp0"

echo ===================================================
echo  Cleaning ALL Caches and Build Artifacts...
echo ===================================================

:: 1. Delete local build / dist directories
if exist "dist" (
    echo  [-] Removing dist\
    rmdir /s /q "dist" 2>nul
)
if exist "build" (
    echo  [-] Removing build\
    rmdir /s /q "build" 2>nul
)
if exist "rackhand.egg-info" (
    echo  [-] Removing rackhand.egg-info\
    rmdir /s /q "rackhand.egg-info" 2>nul
)
if exist ".pytest_cache" (
    rmdir /s /q ".pytest_cache" 2>nul
)
if exist "rackhand.exe" (
    echo  [-] Removing old rackhand.exe
    del /f /q "rackhand.exe" 2>nul
)

:: 2. Delete all __pycache__ folders recursively
echo  [-] Cleaning __pycache__ folders...
for /d /r . %%d in (__pycache__) do @if exist "%%d" rmdir /s /q "%%d" 2>nul

:: 3. Delete PyApp cached runtime & installation on Windows
if defined LOCALAPPDATA (
    if exist "%LOCALAPPDATA%\rackhand" (
        echo  [-] Removing %%LOCALAPPDATA%%\rackhand
        rmdir /s /q "%LOCALAPPDATA%\rackhand" 2>nul
    )
    if exist "%LOCALAPPDATA%\pyapp\rackhand" (
        echo  [-] Removing %%LOCALAPPDATA%%\pyapp\rackhand
        rmdir /s /q "%LOCALAPPDATA%\pyapp\rackhand" 2>nul
    )
)

echo.
echo ===================================================
echo  Compiling Rackhand with PyApp (Rust Standalone)
echo ===================================================
python compile.py --clean %*
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)
echo.
echo [OK] Done! Standalone executable:
echo      rackhand.exe
endlocal
