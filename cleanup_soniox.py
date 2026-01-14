"""
Cleanup script to delete all files and transcriptions from Soniox account.
Run this once to clear the "Too many files" error.
"""
import asyncio
import aiohttp
import os
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.soniox.com/v1"
API_KEY = os.getenv("SONIOX_API_KEY")

async def cleanup():
    if not API_KEY:
        print("ERROR: SONIOX_API_KEY not found in .env")
        return
    
    headers = {"Authorization": f"Bearer {API_KEY}"}
    
    async with aiohttp.ClientSession() as session:
        # List and delete all transcriptions
        print("Fetching transcriptions...")
        try:
            async with session.get(f"{BASE_URL}/transcriptions", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    transcriptions = data.get("transcriptions", data) if isinstance(data, dict) else data
                    if isinstance(transcriptions, list):
                        print(f"Found {len(transcriptions)} transcriptions")
                        for t in transcriptions:
                            tid = t.get("id") if isinstance(t, dict) else t
                            if tid:
                                async with session.delete(f"{BASE_URL}/transcriptions/{tid}", headers=headers) as del_resp:
                                    print(f"  Deleted transcription {tid}: {del_resp.status}")
                else:
                    print(f"Failed to list transcriptions: {resp.status}")
        except Exception as e:
            print(f"Error listing transcriptions: {e}")
        
        # List and delete all files
        print("\nFetching files...")
        try:
            async with session.get(f"{BASE_URL}/files", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    files = data.get("files", data) if isinstance(data, dict) else data
                    if isinstance(files, list):
                        print(f"Found {len(files)} files")
                        for f in files:
                            fid = f.get("id") if isinstance(f, dict) else f
                            if fid:
                                async with session.delete(f"{BASE_URL}/files/{fid}", headers=headers) as del_resp:
                                    print(f"  Deleted file {fid}: {del_resp.status}")
                else:
                    print(f"Failed to list files: {resp.status}")
        except Exception as e:
            print(f"Error listing files: {e}")
        
        print("\nCleanup complete!")

if __name__ == "__main__":
    asyncio.run(cleanup())
