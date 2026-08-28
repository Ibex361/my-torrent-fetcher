"""
ytdlp_and_send.py

Runs inside a GitHub Actions job.
1. Downloads a video/audio from a URL using yt-dlp, at a chosen resolution
   (video) or quality (audio-only).
2. Sends the resulting file to your own Telegram account using Telethon,
   same as the torrent-fetching script, so uploads aren't capped at the
   Bot API's 50MB limit.

Required environment variables (all passed in as GitHub Actions secrets):
  TG_API_ID       - from https://my.telegram.org
  TG_API_HASH     - from https://my.telegram.org
  TG_SESSION      - a Telethon "string session" (created once, see setup script)
  TG_TARGET       - who to send the file to. Usually "me" (your Saved Messages)
  VIDEO_URL       - the page URL to download from (passed as workflow input)
  MODE            - "video" or "audio"
  RESOLUTION      - for video mode: "360", "480", "720", "1080", or "best"
  AUDIO_QUALITY   - for audio mode: "128", "192", "320" (kbps, MP3)
"""

import os
import sys
import glob
import subprocess
import asyncio
import time

# Force unbuffered stdout so print() statements show up immediately in the
# GitHub Actions log instead of being buffered into delayed chunks.
sys.stdout.reconfigure(line_buffering=True)

from telethon import TelegramClient
from telethon.sessions import StringSession

DOWNLOAD_DIR = "/home/runner/ytdlp_downloads"
MAX_WAIT_SECONDS = 5 * 60 * 60  # leave headroom under the 6-hour job limit


def send_status(message: str) -> None:
    """Fire-and-forget a short status message to Telegram."""
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


def build_format_string(mode: str, resolution: str) -> str:
    """
    Build yt-dlp's -f format selector string.
    For video: prefers the requested resolution or lower (so it never
    fails just because the exact resolution isn't available), merges
    best audio, falls back gracefully to 'best' overall if needed.
    """
    if mode == "audio":
        return "bestaudio/best"

    if resolution == "best":
        return "bestvideo+bestaudio/best"

    height = resolution  # e.g. "720"
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"


def download(url: str, mode: str, resolution: str, audio_quality: str) -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    output_template = os.path.join(DOWNLOAD_DIR, "%(title).150s.%(ext)s")

    cmd = ["yt-dlp", "--newline", "-o", output_template]

    if mode == "audio":
        print(f"=== DOWNLOADING AUDIO === {url} (target quality: {audio_quality}kbps MP3)")
        cmd += [
            "-x",                              # extract audio only
            "--audio-format", "mp3",
            "--audio-quality", audio_quality,  # kbps target for MP3
            "-f", build_format_string(mode, resolution),
        ]
    else:
        print(f"=== DOWNLOADING VIDEO === {url} (target: {resolution})")
        cmd += [
            "-f", build_format_string(mode, resolution),
            "--merge-output-format", "mp4",
            "--embed-subs",       # keep subtitles if the source has them
            "--sub-langs", "all",
            "--write-sub",
            "--write-auto-sub",
        ]

    cmd.append(url)

    print(f"Running: {' '.join(cmd)}")

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    last_status_ping = time.time()
    for line in process.stdout:
        line = line.rstrip()
        if line:
            print(line)
        # Periodically ping Telegram so this doesn't look stuck on long downloads
        now = time.time()
        if now - last_status_ping >= 60 and ("[download]" in line and "%" in line):
            last_status_ping = now
            send_status(f"⏳ Still downloading...\n{line.strip()}")

    returncode = process.wait(timeout=MAX_WAIT_SECONDS)
    if returncode != 0:
        print(f"yt-dlp exited with code {returncode}")
        sys.exit(1)

    print("=== DOWNLOAD DONE ===")


def find_downloaded_files():
    all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
    # Skip leftover partial/temp files yt-dlp sometimes leaves behind on retries
    files = [
        f for f in all_files
        if os.path.isfile(f) and not f.endswith((".part", ".ytdl", ".ytdl.part"))
    ]
    return files


async def send_files(files, mode: str, resolution: str, audio_quality: str):
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
        if mode == "audio":
            label = f"🎵 Audio ({audio_quality}kbps MP3)"
        else:
            label = f"🎬 Video ({resolution})"
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
    url = os.environ.get("VIDEO_URL", "").strip()
    if not url:
        print("No VIDEO_URL provided.")
        sys.exit(1)

    mode = os.environ.get("MODE", "video").strip().lower()
    resolution = os.environ.get("RESOLUTION", "best").strip()
    audio_quality = os.environ.get("AUDIO_QUALITY", "192").strip()

    label = f"audio ({audio_quality}kbps)" if mode == "audio" else f"video ({resolution})"
    send_status(f"🚀 yt-dlp job started — {label}\n{url}")

    download(url, mode, resolution, audio_quality)
    files = find_downloaded_files()
    print(f"Found {len(files)} file(s) to send.")
    send_status(f"📥 Download complete — {len(files)} file(s). Uploading now...")

    asyncio.run(send_files(files, mode, resolution, audio_quality))


if __name__ == "__main__":
    main()
