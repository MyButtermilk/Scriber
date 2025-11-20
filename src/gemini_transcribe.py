import os
import sys
from loguru import logger

# This script demonstrates how one might use Google Gemini for audio transcription
# as an "offline" or "batch" alternative to the streaming pipeline.
# It requires the `google-generativeai` library.

def transcribe_audio_with_gemini(audio_path: str):
    """
    Uploads an audio file to Gemini and requests a transcription.
    """
    try:
        import google.generativeai as genai
    except ImportError:
        logger.error("google-generativeai not installed.")
        return

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("GOOGLE_API_KEY not found.")
        return

    genai.configure(api_key=api_key)

    if not os.path.exists(audio_path):
        logger.error(f"File not found: {audio_path}")
        return

    logger.info(f"Uploading {audio_path} to Gemini...")
    try:
        # Upload the file
        myfile = genai.upload_file(audio_path)

        # Initialize model
        model = genai.GenerativeModel("gemini-1.5-flash")

        # Generate content
        logger.info("Requesting transcription...")
        result = model.generate_content(
            [myfile, "Transcribe this audio file into text."]
        )

        logger.info(f"Transcription Result:\n{result.text}")
        return result.text

    except Exception as e:
        logger.error(f"Gemini Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/gemini_transcribe.py <path_to_audio_file>")
    else:
        transcribe_audio_with_gemini(sys.argv[1])
