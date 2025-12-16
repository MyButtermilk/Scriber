#!/bin/bash

echo "---------------------------------------------------"
echo "      Scriber - Voice Dictation (Linux/Mac)"
echo "---------------------------------------------------"

# 1. Check for Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python 3 is not installed."
    exit 1
fi

# 2. Setup Virtual Environment
if [ ! -d "venv" ]; then
    echo "[INFO] Creating virtual environment..."
    python3 -m venv venv
fi

# 3. Activate Virtual Environment
source venv/bin/activate

# 4. Install Dependencies (re-run when requirements.txt changes)
REQ_HASH_FILE="venv/requirements.sha256"
REQ_HASH=""

if command -v sha256sum &> /dev/null; then
    REQ_HASH=$(sha256sum requirements.txt | awk '{print $1}')
elif command -v shasum &> /dev/null; then
    REQ_HASH=$(shasum -a 256 requirements.txt | awk '{print $1}')
fi

NEED_INSTALL=1
if [ -n "$REQ_HASH" ] && [ -f "$REQ_HASH_FILE" ]; then
    OLD_HASH=$(cat "$REQ_HASH_FILE")
    if [ "$OLD_HASH" = "$REQ_HASH" ]; then
        NEED_INSTALL=0
    fi
fi

if [ "$NEED_INSTALL" -eq 1 ]; then
    echo "[INFO] Installing dependencies..."
    pip install -r requirements.txt
    if [ -n "$REQ_HASH" ]; then
        echo "$REQ_HASH" > "$REQ_HASH_FILE"
    fi
else
    echo "[INFO] Dependencies up to date. Skipping..."
fi

# 5. Configuration Setup
if [ ! -f ".env" ]; then
    echo ""
    echo "[SETUP] Configuration not found."
    read -p "Enter Soniox API Key: " SONIOX
    read -p "Enter AssemblyAI API Key: " ASSEMBLY
    read -p "Enter Deepgram API Key: " DEEPGRAM
    read -p "Enter OpenAI API Key: " OPENAI
    read -p "Enter Azure Speech Key: " AZURE_KEY
    read -p "Enter Azure Speech Region: " AZURE_REGION
    read -p "Enter Gladia API Key: " GLADIA
    read -p "Enter ElevenLabs API Key: " ELEVEN

    cat <<EOT > .env
SONIOX_API_KEY=$SONIOX
ASSEMBLYAI_API_KEY=$ASSEMBLY
ELEVENLABS_API_KEY=$ELEVEN
DEEPGRAM_API_KEY=$DEEPGRAM
OPENAI_API_KEY=$OPENAI
AZURE_SPEECH_KEY=$AZURE_KEY
AZURE_SPEECH_REGION=$AZURE_REGION
GLADIA_API_KEY=$GLADIA
SCRIBER_HOTKEY=ctrl+alt+s
SCRIBER_DEFAULT_STT=soniox
SCRIBER_CUSTOM_VOCAB=Scriber, Pipecat, Soniox
EOT
    echo "[INFO] .env file created."
fi

# 6. Run the App
echo ""
echo "[INFO] Starting Scriber..."
python -m src.main
