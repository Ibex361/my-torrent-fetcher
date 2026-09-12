"""
fetch_and_send.py

Runs inside a GitHub Actions job.
1. Downloads a torrent (from a magnet link) using peerflix (pure-JS,
   no native dependencies — see download_torrent() for why aria2/
   webtorrent-cli were dropped in favor of this).
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
  COMPRESS        - optional, set to "true" to re-encode to x265 (HEVC) before sending
"""

import os
import sys
import glob
import subprocess
import asyncio

# Force unbuffered stdout so print() statements show up immediately in the
# GitHub Actions log instead of being buffered and appearing in delayed
# chunks (this was making long-running steps like compression look frozen
# even though they were working fine underneath).
sys.stdout.reconfigure(line_buffering=True)

from telethon import TelegramClient
from telethon.sessions import StringSession

DOWNLOAD_DIR = "/home/runner/downloads"
COMPRESSED_DIR = "/home/runner/compressed"
MAX_WAIT_SECONDS = 5 * 60 * 60  # leave headroom under the 6-hour job limit
PREVIEW_MB = 10                  # how many MB to grab in preview mode
CRF = 22                         # x265 quality: lower = better quality/bigger file. ~20-23 is visually near-lossless
COMPRESS_TIMEOUT_SECONDS = 4 * 60 * 60  # cap encode time so it can't eat the whole job


def download_torrent(magnet_link: str, preview_only: bool = False) -> None:
    """
    Downloads a torrent using peerflix instead of aria2.

    Why the switch: aria2 repeatedly failed to even fetch torrent metadata
    for several magnet links on GitHub's runners (stuck at 0 connections,
    0 bytes downloaded, indefinitely) despite those same torrents having
    healthy seed counts and downloading instantly from a phone/home network.

    peerflix is a pure-JavaScript BitTorrent client (no native/compiled
    dependencies, unlike webtorrent-cli's newer versions which currently
    have a broken native WebRTC addon). It works as a local streaming
    server: it downloads pieces to a temp buffer directory and serves them
    over local HTTP. We don't use the streaming/HTTP part at all here —
    we just let it download to disk and read the finished file directly
    from its buffer path once done.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if preview_only:
        print(f"PREVIEW MODE: will stop after ~{PREVIEW_MB}MB of downloaded data")
        _run_peerflix(magnet_link, limit_bytes=PREVIEW_MB * 1024 * 1024)
    else:
        print(f"=== DOWNLOADING (peerflix) === {magnet_link}")
        _run_peerflix(magnet_link, limit_bytes=None)
        print("=== DOWNLOAD DONE ===")


def _run_peerflix(magnet_link: str, limit_bytes) -> None:
    """
    Runs peerflix, which downloads into a buffer folder and serves it over
    local HTTP (we ignore the HTTP part). Polls the buffer folder for
    progress and either:
      - stops once `limit_bytes` of real data has been written (preview mode), or
      - waits for peerflix to report the torrent fully downloaded (full mode).
    Once stopped, copies the resulting file(s) into DOWNLOAD_DIR.
    """
    import time
    import signal
    import re
    import shutil
    import select

    buffer_dir = "/home/runner/.peerflix-buffer"
    os.makedirs(buffer_dir, exist_ok=True)

    cmd = [
        "peerflix", magnet_link,
        "--path", buffer_dir,
        "--all",       # download every file in the torrent, not just the biggest
        "--port", "8888",
    ]

    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )

    waited = 0
    last_print = 0.0
    fully_downloaded = False

    while process.poll() is None:
        # Non-blocking read: wait up to 1s for output, but always fall through
        # to the progress/size checks below even if peerflix stays quiet.
        ready, _, _ = select.select([process.stdout], [], [], 1)
        if ready:
            line = process.stdout.readline()
            if line:
                line = line.rstrip()
                if line:
                    print(line)
                if re.search(r"100(\.0+)?%", line) or "download complete" in line.lower():
                    fully_downloaded = True

        waited += 1

        now = time.time()
        if now - last_print >= 10:
            last_print = now
            all_files = glob.glob(os.path.join(buffer_dir, "**", "*"), recursive=True)
            current_files = [f for f in all_files if os.path.isfile(f)]
            total_bytes = sum(os.path.getsize(f) for f in current_files)
            print(f"  [peerflix] {total_bytes / (1024*1024):.1f} MB written so far (elapsed {waited}s)")

            if limit_bytes is not None and total_bytes >= limit_bytes:
                print(f"Preview size reached ({total_bytes / (1024*1024):.1f} MB), stopping peerflix.")
                _stop_process(process)
                break

            if limit_bytes is None and fully_downloaded:
                print("peerflix reports the download is complete.")
                _stop_process(process)
                break

        if waited > MAX_WAIT_SECONDS:
            print("Max wait time reached, stopping peerflix.")
            _stop_process(process)
            break

    # Copy whatever was downloaded into DOWNLOAD_DIR for the rest of the pipeline
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(buffer_dir, "**", "*"), recursive=True):
        if os.path.isfile(f):
            rel = os.path.relpath(f, buffer_dir)
            dest = os.path.join(DOWNLOAD_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(f, dest)

    # In preview mode, truncate the copied file(s) down to the target size —
    # peerflix downloads pieces in whatever order it fetches them (not
    # strictly sequential by default), but for a short preview window this
    # is very likely to still be front-loaded content; good enough for a
    # sanity-check preview.
    if limit_bytes is not None:
        for f in find_downloaded_files():
            if os.path.getsize(f) > limit_bytes:
                with open(f, "r+b") as fh:
                    fh.truncate(limit_bytes)
                print(f"Truncated {f} to {limit_bytes / (1024*1024):.1f} MB for preview.")


def _stop_process(process) -> None:
    import signal
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:
        pass


def find_downloaded_files():
    all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*"), recursive=True)
    files = [f for f in all_files if os.path.isfile(f)]
    return files


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv"}


def compress_video(input_path: str) -> str:
    """
    Re-encode a video to x265 (HEVC) with AAC audio, at a quality level
    that's visually/aurally very close to the original but noticeably
    smaller. Returns the path to the compressed file, or the original
    path unchanged if compression isn't applicable/fails.

    Prints live progress (% complete, speed, ETA) every few seconds by
    reading ffmpeg's machine-readable -progress output, so this doesn't
    look "stuck" for however long the encode takes.
    """
    import re
    import time

    ext = os.path.splitext(input_path)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        print(f"Skipping compression for non-video file: {input_path}")
        return input_path

    os.makedirs(COMPRESSED_DIR, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    # MKV instead of MP4: MP4 has poor/no support for most subtitle formats,
    # so embedded subs would get silently dropped even if we mapped them.
    output_path = os.path.join(COMPRESSED_DIR, f"{base_name}.x265.mkv")

    original_mb = os.path.getsize(input_path) / (1024 * 1024)

    # Get total duration first, so we can compute a % complete
    print("Checking video duration...")
    duration_seconds = _get_video_duration(input_path)
    dur_str = f"{duration_seconds/60:.1f} min" if duration_seconds else "unknown length"
    print(f"=== COMPRESSING === {input_path} ({original_mb:.1f} MB, {dur_str}) -> x265, CRF {CRF}")
    print("(this can take a while — progress updates below every ~10s)")
    print("Starting ffmpeg (keeping subtitles + all audio tracks)...")

    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-map", "0",                # include ALL streams from input: video, every audio track, every subtitle track, chapters
        "-c:v", "libx265",
        "-crf", str(CRF),
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "128k",
        "-c:s", "copy",             # copy subtitle streams as-is, no re-encoding needed
        "-max_muxing_queue_size", "9999",  # avoids a common ffmpeg error when muxing multiple stream types
        "-progress", "pipe:1",   # machine-readable progress on stdout
        "-nostats",
        "-y",
        output_path,
    ]

    start_time = time.time()
    last_print = 0.0
    current_out_time = 0.0
    current_speed = ""

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        import select

        while True:
            # Wait up to 10s for new output; if none arrives, we still get
            # a chance to print a heartbeat below instead of blocking silently.
            ready, _, _ = select.select([process.stdout], [], [], 10)

            if ready:
                line = process.stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    continue
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        current_out_time = int(line.split("=")[1]) / 1_000_000
                    except ValueError:
                        pass
                elif line.startswith("speed="):
                    current_speed = line.split("=")[1]

            now = time.time()
            if now - last_print >= 10:
                last_print = now
                elapsed_min = (now - start_time) / 60
                if duration_seconds:
                    pct = min(100, current_out_time / duration_seconds * 100)
                    eta_min = ((now - start_time) / max(current_out_time, 0.01)) * (duration_seconds - current_out_time) / 60
                    print(f"  [compress] {pct:.0f}% | speed {current_speed or '?'} | elapsed {elapsed_min:.1f}m | ETA ~{eta_min:.1f}m")
                else:
                    print(f"  [compress] {current_out_time/60:.1f} min encoded | speed {current_speed or '?'} | elapsed {elapsed_min:.1f}m")

            if process.poll() is not None:
                break

        returncode = process.wait(timeout=COMPRESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        print("Compression timed out, sending original file instead.")
        return input_path

    if returncode != 0 or not os.path.exists(output_path):
        print(f"ffmpeg failed (exit {returncode}), sending original file instead.")
        return input_path

    compressed_mb = os.path.getsize(output_path) / (1024 * 1024)
    savings_pct = (1 - compressed_mb / original_mb) * 100 if original_mb else 0
    total_min = (time.time() - start_time) / 60
    print(f"=== COMPRESSION DONE === {compressed_mb:.1f} MB ({savings_pct:.0f}% smaller), took {total_min:.1f} min")
    return output_path


def _get_video_duration(path: str):
    """Return video duration in seconds using ffprobe, or None if unknown."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


async def send_files(files, preview_only: bool = False, compressed: bool = False):
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
        if preview_only:
            label = "⚠️ PREVIEW (partial file)"
        elif compressed:
            label = "✅ Full file (compressed to x265)"
        else:
            label = "✅ Full file"
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


def send_status(message: str) -> None:
    """Fire-and-forget a short status message to Telegram, so progress is
    visible from your phone without needing to open the GitHub Actions log.
    Failures here are non-fatal — never let a status ping crash the job."""
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


def main():
    magnet_link = os.environ.get("MAGNET_LINK", "").strip()
    if not magnet_link:
        print("No MAGNET_LINK provided.")
        sys.exit(1)

    preview_only = os.environ.get("PREVIEW_ONLY", "false").strip().lower() == "true"
    compress = os.environ.get("COMPRESS", "false").strip().lower() == "true"

    mode = "PREVIEW" if preview_only else ("FULL + COMPRESS" if compress else "FULL")
    send_status(f"🚀 Job started ({mode})\nDownloading torrent now...")

    download_torrent(magnet_link, preview_only=preview_only)
    files = find_downloaded_files()
    print(f"Found {len(files)} file(s) to send.")
    send_status(f"📥 Download complete — {len(files)} file(s) found.")

    compressed_applied = False
    if compress and not preview_only:
        send_status("🎬 Compressing to x265 now — this can take a while, will update when done.")
        new_files = []
        for f in files:
            result_path = compress_video(f)
            new_files.append(result_path)
            if result_path != f:
                compressed_applied = True
        files = new_files
        if compressed_applied:
            send_status("✅ Compression done — uploading now.")
        else:
            send_status("⚠️ Compression skipped/failed — uploading original file instead.")
    else:
        send_status("📤 Uploading now...")

    asyncio.run(send_files(files, preview_only=preview_only, compressed=compressed_applied))


if __name__ == "__main__":
    main()
