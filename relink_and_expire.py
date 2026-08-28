"""
relink_and_expire.py

Runs inside a GitHub Actions job.
1. Looks at your own Telegram "Saved Messages" and finds the most recent
   message that has a file attached (forwarded from wherever).
2. Downloads that file via Telethon (your logged-in account).
3. Uploads it as a GitHub Release asset -> gives a direct, ADM-downloadable
   public link (since this repo is public).
4. Sends you that link on Telegram.
5. Waits 3 hours, then deletes the Release + the git tag, so the link stops
   working and nothing lingers in your repo's Releases page.

Required environment variables:
  TG_API_ID, TG_API_HASH, TG_SESSION, TG_TARGET  - same as the other scripts
  GH_TOKEN        - a GitHub token with repo access (GITHUB_TOKEN works fine,
                     provided automatically by Actions - see workflow yml)
  GH_REPO         - "owner/repo", e.g. "Ibex361/my-torrent-fetcher"
  EXPIRE_SECONDS  - how long to keep the file up before deleting (default 10800 = 3h)
"""

import os
import sys
import time
import json
import asyncio
import subprocess

sys.stdout.reconfigure(line_buffering=True)

from telethon import TelegramClient
from telethon.sessions import StringSession

DOWNLOAD_DIR = "/home/runner/relink_downloads"


def send_status(message: str) -> None:
    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
        session_str = os.environ["TG_SESSION"]
        target = os.environ.get("TG_TARGET", "me")

        async def _send():
            client = TelegramClient(StringSession(session_str), api_id, api_hash)
            await client.start()
            await client.send_message(target, message)
            await client.disconnect()

        asyncio.run(_send())
    except Exception as e:
        print(f"(status ping failed, continuing anyway: {e})")


async def fetch_latest_file() -> str:
    """Download the most recent file-bearing message from Saved Messages.
    Returns the local path to the downloaded file."""
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_str = os.environ["TG_SESSION"]

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.start()

    print("Scanning Saved Messages for the most recent file...")
    target_message = None
    async for message in client.iter_messages("me", limit=50):
        if message.file:  # has any attached media/document
            target_message = message
            break

    if target_message is None:
        await client.disconnect()
        raise RuntimeError("No file found in the last 50 messages of Saved Messages.")

    filename = target_message.file.name or f"file_{target_message.id}"
    print(f"Found: {filename} ({target_message.file.size / (1024*1024):.1f} MB) — downloading...")

    def progress(current, total):
        if total:
            pct = current / total * 100
            if int(pct) % 10 == 0:
                print(f"  download {pct:.0f}%")

    path = await target_message.download_media(file=DOWNLOAD_DIR + "/", progress_callback=progress)
    await client.disconnect()

    print(f"Downloaded to: {path}")
    return path


def create_release_with_asset(file_path: str):
    """Create a uniquely-tagged GitHub Release and upload the file as an asset.
    Returns (tag_name, asset_download_url)."""
    repo = os.environ["GH_REPO"]
    tag = f"temp-{int(time.time())}"
    title = "Temporary file (auto-deletes in a few hours)"

    print(f"Creating GitHub Release {tag} on {repo}...")
    subprocess.run(
        ["gh", "release", "create", tag, file_path, "--repo", repo, "--title", title,
         "--notes", "Auto-generated temporary download link. Will be deleted automatically."],
        check=True,
    )

    result = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"],
        check=True, capture_output=True, text=True,
    )
    assets = json.loads(result.stdout)["assets"]
    if not assets:
        raise RuntimeError("Release created but no asset URL found.")
    download_url = assets[0]["url"]

    return tag, download_url


def delete_release(tag: str) -> None:
    repo = os.environ["GH_REPO"]
    print(f"Deleting release {tag}...")
    subprocess.run(["gh", "release", "delete", tag, "--repo", repo, "--yes", "--cleanup-tag"], check=True)


def main():
    expire_seconds = int(os.environ.get("EXPIRE_SECONDS", "10800"))  # 3 hours default

    send_status("🔎 Looking for the most recent file in Saved Messages...")

    file_path = asyncio.run(fetch_latest_file())

    send_status("📤 Uploading to GitHub as a temporary download link...")
    tag, download_url = create_release_with_asset(file_path)

    expire_hours = expire_seconds / 3600
    send_status(
        f"✅ Direct download link (works in ADM/browsers, no login needed):\n{download_url}\n\n"
        f"⏳ This link will stop working in {expire_hours:.1f} hours."
    )
    print(f"Link ready: {download_url}")
    print(f"Sleeping for {expire_seconds} seconds before deleting release...")

    time.sleep(expire_seconds)

    delete_release(tag)
    send_status("🗑️ The temporary link has expired and been deleted.")
    print("Done.")


if __name__ == "__main__":
    main()
