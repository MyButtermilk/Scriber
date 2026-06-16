@echo off
setlocal

title Scriber - Tauri Dev
echo ---------------------------------------------------
echo       Scriber - Tauri Desktop Dev
echo ---------------------------------------------------

where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Node.js is not installed or not in PATH.
    echo Install Node.js, then run this script again.
    pause
    exit /b 1
)

where cargo >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Rust/Cargo is not installed or not in PATH.
    echo Install the Rust toolchain, then run this script again.
    pause
    exit /b 1
)

if not exist "Frontend" (
    echo [ERROR] Frontend folder not found. Run this from the repository root.
    pause
    exit /b 1
)

pushd Frontend
if not exist "node_modules" (
    echo [INFO] Installing frontend dependencies...
    call npm install
    if errorlevel 1 (
        popd
        echo [ERROR] npm install failed.
        pause
        exit /b 1
    )
)

echo [INFO] Starting Scriber through Tauri...
call npm run tauri:dev
set "EXIT_CODE=%errorlevel%"
popd

if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Tauri dev runtime stopped with exit code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
