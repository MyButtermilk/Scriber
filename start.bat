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

REM 4. Install Dependencies
if not exist "venv\installed.flag" (
    echo [INFO] Installing dependencies... This may take a minute.
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b
    )
    echo done > venv\installed.flag
) else (
    echo [INFO] Dependencies already installed. Skipping...
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

REM 6. Run the App
echo.
echo [INFO] Starting Scriber...
echo        Press Ctrl+Alt+S to dictate.
echo.

REM Run as a module to handle imports correctly
python -m src.main

if %errorlevel% neq 0 (
    echo [ERROR] Application crashed or stopped with error.
    pause
)
