@echo off
setlocal
cd /d "%~dp0"
echo ===================================================
echo  Compiling Rackhand with PyApp (Rust Standalone)
echo ===================================================
python compile.py %*
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)
echo.
echo [OK] Done! Standalone executable:
echo      rackhand.exe
endlocal
