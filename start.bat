@echo off
setlocal EnableDelayedExpansion

title Scriber - Voice Dictation
echo ---------------------------------------------------
echo       Scriber - Windows Voice Dictation
echo ---------------------------------------------------

REM 1. Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in your PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and ensure "Add Python to PATH" is checked.
    pause
    exit /b
)

REM 2. Setup Virtual Environment
if not exist "venv" (
    echo [INFO] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b
    )
)

REM 3. Activate Virtual Environment
call venv\Scripts\activate
if %errorlevel% neq 0 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b
)

REM 4. Install Dependencies (re-run when requirements.txt changes)
set "REQ_HASH="
for /f %%h in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -Algorithm SHA256 requirements.txt).Hash"') do set "REQ_HASH=%%h"
set "REQ_HASH=!REQ_HASH: =!"
set "REQ_HASH=!REQ_HASH:	=!"
set "HASH_FILE=venv\requirements.sha256"
set "OLD_HASH="
if exist "%HASH_FILE%" (
    for /f "usebackq delims=" %%h in ("%HASH_FILE%") do set "OLD_HASH=%%h"
    set "OLD_HASH=!OLD_HASH: =!"
    set "OLD_HASH=!OLD_HASH:	=!"
)
if "%REQ_HASH%"=="" (
    echo [WARN] Could not compute requirements hash. Installing dependencies...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b
    )
) else if /I not "!OLD_HASH!"=="!REQ_HASH!" (
    echo [INFO] Installing dependencies... This may take a minute.
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b
    )
    > "%HASH_FILE%" (echo(!REQ_HASH!)
) else (
    echo [INFO] Dependencies up to date. Skipping...
)

REM 5. Configuration Setup
if not exist ".env" (
    echo.
    echo [SETUP] Configuration not found. Let's set it up.
    echo Press Enter to skip any service you don't have.
    echo.

    set /p SONIOX="Enter Soniox API Key: "
    set /p ASSEMBLY="Enter AssemblyAI API Key: "
    set /p DEEPGRAM="Enter Deepgram API Key: "
    set /p OPENAI="Enter OpenAI API Key: "
    set /p AZURE_KEY="Enter Azure Speech Key: "
    set /p AZURE_REGION="Enter Azure Speech Region: "
    set /p GLADIA="Enter Gladia API Key: "
    set /p ELEVEN="Enter ElevenLabs API Key: "
    set /p GOOGLE="Enter path to Google Cloud JSON credentials: "

    (
        echo SONIOX_API_KEY=!SONIOX!
        echo ASSEMBLYAI_API_KEY=!ASSEMBLY!
        echo ELEVENLABS_API_KEY=!ELEVEN!
        echo GOOGLE_APPLICATION_CREDENTIALS=!GOOGLE!
        echo DEEPGRAM_API_KEY=!DEEPGRAM!
        echo OPENAI_API_KEY=!OPENAI!
        echo AZURE_SPEECH_KEY=!AZURE_KEY!
        echo AZURE_SPEECH_REGION=!AZURE_REGION!
        echo GLADIA_API_KEY=!GLADIA!
        echo.
        echo # Configuration
        echo SCRIBER_HOTKEY=ctrl+alt+s
        echo SCRIBER_DEFAULT_STT=soniox
        echo SCRIBER_CUSTOM_VOCAB=Scriber, Pipecat, Soniox
    ) > .env

    echo.
    echo [INFO] .env file created. You can edit it later.
)

REM Read hotkey from .env for display
set "HOTKEY_DISPLAY=Ctrl+Alt+S"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if /I "%%a"=="SCRIBER_HOTKEY" set "HOTKEY_DISPLAY=%%b"
    )
)

REM 6. Run the App
echo.
echo [INFO] Starting Scriber...
echo        Hotkey: !HOTKEY_DISPLAY!
echo.

REM Prefer the new Web UI (React) if Node.js is available; fall back to Tkinter UI otherwise.
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Node.js not found. Starting legacy desktop UI - Tkinter...
    echo.
    python -m src.main
    goto :after_run
)

if not exist "Frontend" (
    echo [WARN] Frontend folder not found. Starting legacy desktop UI - Tkinter...
    echo.
    python -m src.main
    goto :after_run
)

REM Install frontend dependencies if needed
if not exist "Frontend\\node_modules" (
    echo [INFO] Installing frontend dependencies... This may take a minute.
    pushd Frontend
    npm install
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install frontend dependencies.
        popd
        pause
        exit /b
    )
    popd
) else (
    echo [INFO] Frontend dependencies already installed. Skipping...
)

echo.
echo [INFO] Starting backend (Python) on http://127.0.0.1:8765 ...
start "Scriber Backend" cmd /c "python -m src.web_api || pause"

echo [INFO] Waiting for backend to become ready...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$u='http://127.0.0.1:8765/api/health'; for($i=0;$i -lt 40;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 $u; if($r.StatusCode -eq 200){ exit 0 } } catch { } Start-Sleep -Milliseconds 250 }; exit 1"
if %errorlevel% neq 0 (
    echo [ERROR] Backend did not start.
    echo        Check the Scriber Backend window for errors.
    pause
    exit /b 1
)

echo [INFO] Starting Web UI on http://localhost:5000 ...
echo.
pushd Frontend
set "VITE_BACKEND_URL=http://127.0.0.1:8765"
start "" http://localhost:5000
npm run dev:client
popd

:after_run
if %errorlevel% neq 0 (
    echo [ERROR] Application crashed or stopped with error.
    pause
)
