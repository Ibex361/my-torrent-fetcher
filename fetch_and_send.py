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
  PREVIEW_ONLY    - optional, set to "true" to download only ~10MB as a preview
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
PREVIEW_MB = 10                  # how many MB to grab in preview mode


def download_torrent(magnet_link: str, preview_only: bool = False) -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if preview_only:
        print(f"PREVIEW MODE: will stop after ~{PREVIEW_MB}MB")

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

    if preview_only:
        # aria2 doesn't have a native "stop after N bytes" flag for torrents,
        # so we run it in a thread and kill it once any file reaches PREVIEW_MB.
        _download_with_size_limit(cmd)
    else:
        print(f"Starting full download for: {magnet_link}")
        result = subprocess.run(cmd, timeout=MAX_WAIT_SECONDS)
        if result.returncode not in (0, -15):  # -15 = SIGTERM (we sent it), that's fine
            print(f"aria2c exited with code {result.returncode}")
            sys.exit(1)


def _download_with_size_limit(cmd: list) -> None:
    """Start aria2c and kill it once the largest downloaded file hits PREVIEW_MB."""
    import time
    import signal

    process = subprocess.Popen(cmd)
    limit_bytes = PREVIEW_MB * 1024 * 1024
    waited = 0

    while process.poll() is None:  # while aria2 is still running
        time.sleep(5)
        waited += 5

        # Check size of all files downloaded so far
        all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*"), recursive=True)
        partial_files = [f for f in all_files if os.path.isfile(f)]
        for f in partial_files:
            try:
                if os.path.getsize(f) >= limit_bytes:
                    print(f"Preview size reached on {os.path.basename(f)}, stopping aria2c.")
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=10)
                    return
            except OSError:
                pass

        if waited > 3600:  # 1 hour safety cap for preview mode
            print("Preview timeout reached, stopping.")
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
            return

    print(f"aria2c finished (exit code {process.returncode})")


def find_downloaded_files():
    # aria2 also creates .aria2 control files while downloading; ignore those
    all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*"), recursive=True)
    files = [f for f in all_files if os.path.isfile(f) and not f.endswith(".aria2")]
    return files


async def send_files(files, preview_only: bool = False):
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

    # In preview mode, only send the largest file (the actual video, not tiny sidecar files)
    if preview_only:
        files = [max(files, key=os.path.getsize)]
        await client.send_message(target, f"👀 Preview mode — sending first ~{PREVIEW_MB}MB of the file so you can check it.")

    for f in files:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        label = "⚠️ PREVIEW (partial file)" if preview_only else "✅ Full file"
        print(f"Uploading {f} ({size_mb:.1f} MB)...")
        try:
            await client.send_file(
                target,
                f,
                caption=f"{label}\n{os.path.basename(f)} ({size_mb:.1f} MB)",
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

    preview_only = os.environ.get("PREVIEW_ONLY", "false").strip().lower() == "true"

    download_torrent(magnet_link, preview_only=preview_only)
    files = find_downloaded_files()
    print(f"Found {len(files)} file(s) to send.")

    asyncio.run(send_files(files, preview_only=preview_only))


if __name__ == "__main__":
    main()
