"""
fetch_and_send.py

Runs inside a GitHub Actions job.
1. Downloads a torrent (from a magnet link) using aria2.
2. Sends the resulting file(s) to your own Telegram account using Telethon
   (a user-account library), which allows uploads up to ~2GB (4GB with
   Telegram Premium) instead of the 50MB limit the normal Bot API has.

Required environment variables (all passed in as GitHub Actions secrets):
  TG_API_ID       - from https://my.telegram.org
  TG_API_HASH     - from https://my.telegram.org
  TG_SESSION      - a Telethon "string session" (created once, see setup script)
  TG_TARGET       - who to send the file to. Usually "me" (your Saved Messages)
  MAGNET_LINK     - the magnet link to download (passed as workflow input)
"""

import os
import sys
import glob
import subprocess
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

DOWNLOAD_DIR = "/home/runner/downloads"
MAX_WAIT_SECONDS = 5 * 60 * 60  # leave headroom under the 6-hour job limit


def download_torrent(magnet_link: str) -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"Starting download for: {magnet_link}")

    cmd = [
        "aria2c",
        "--seed-time=0",              # don't seed after finishing, just exit
        "--bt-stop-timeout=60",       # give up if stuck with zero progress for 60s
        "--max-tries=3",
        "--dir", DOWNLOAD_DIR,
        "--summary-interval=30",
        "--console-log-level=warn",
        magnet_link,
    ]

    result = subprocess.run(cmd, timeout=MAX_WAIT_SECONDS)
    if result.returncode != 0:
        print(f"aria2c exited with code {result.returncode}")
        sys.exit(1)


def find_downloaded_files():
    # aria2 also creates .aria2 control files while downloading; ignore those
    all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*"), recursive=True)
    files = [f for f in all_files if os.path.isfile(f) and not f.endswith(".aria2")]
    return files


async def send_files(files):
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_str = os.environ["TG_SESSION"]
    target = os.environ.get("TG_TARGET", "me")

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.start()

    if not files:
        await client.send_message(target, "⚠️ Download finished but no files were found.")
        await client.disconnect()
        return

    for f in files:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        print(f"Uploading {f} ({size_mb:.1f} MB)...")
        try:
            await client.send_file(
                target,
                f,
                caption=f"{os.path.basename(f)} ({size_mb:.1f} MB)",
                progress_callback=lambda sent, total: print(
                    f"  {sent / total * 100:.0f}%"
                ) if total else None,
            )
            print(f"Done: {f}")
        except Exception as e:
            await client.send_message(target, f"❌ Failed to upload {os.path.basename(f)}: {e}")

    await client.disconnect()


def main():
    magnet_link = os.environ.get("MAGNET_LINK", "").strip()
    if not magnet_link:
        print("No MAGNET_LINK provided.")
        sys.exit(1)

    download_torrent(magnet_link)
    files = find_downloaded_files()
    print(f"Found {len(files)} file(s) to send.")

    asyncio.run(send_files(files))


if __name__ == "__main__":
    main()
