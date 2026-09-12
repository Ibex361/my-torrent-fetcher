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
  COMPRESS        - optional, set to "true" to re-encode to x265 (HEVC) before sending
"""

import os
import sys
import glob
import subprocess
import asyncio
import urllib.parse

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


# Well-known public trackers that work over HTTP/HTTPS (i.e. plain TCP/443),
# appended to every magnet link as a fallback. If GitHub's network drops or
# throttles UDP (which most BitTorrent trackers and all of DHT rely on),
# these give aria2 an alternative way to actually find peers.
HTTP_FALLBACK_TRACKERS = [
    "https://tracker.opentrackr.org:443/announce",
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.gbitt.info/announce",
    "http://tracker.gbitt.info/announce",
    "https://tracker.tamersunion.org:443/announce",
    "http://open.acgnxtracker.com:80/announce",
    "http://tracker.files.fm:6969/announce",
]


def _add_fallback_trackers(magnet_link: str) -> str:
    """Append HTTP/HTTPS trackers to a magnet link's existing tracker list.
    Magnet links use repeated &tr= params, so this just adds more."""
    extra = "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in HTTP_FALLBACK_TRACKERS)
    return magnet_link + extra


def download_torrent(magnet_link: str, preview_only: bool = False) -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    magnet_link = _add_fallback_trackers(magnet_link)
    print(f"(added {len(HTTP_FALLBACK_TRACKERS)} HTTP/HTTPS fallback trackers for peer-discovery resilience)")

    if preview_only:
        print(f"PREVIEW MODE: will stop after ~{PREVIEW_MB}MB of REAL downloaded data")
        _download_with_size_limit(magnet_link)
    else:
        print(f"=== DOWNLOADING === {magnet_link}")
        cmd = [
            "aria2c",
            "--seed-time=0",
            "--bt-stop-timeout=180",
            "--max-tries=3",
            "--dir", DOWNLOAD_DIR,
            "--summary-interval=15",
            "--console-log-level=warn",
            # --- Peer-discovery resilience flags ---
            # Some torrents rely mostly on DHT to find peers rather than
            # trackers. GitHub's runners can be slow/unreliable to bootstrap
            # into the DHT network over UDP, so we give it more nodes to try
            # and more time before giving up.
            "--enable-dht=true",
            "--enable-dht6=false",
            "--dht-listen-port=6881-6999",
            "--bt-enable-lpd=true",           # local peer discovery, harmless extra option
            "--peer-id-prefix=-TR2940-",       # some trackers/peers are picky about client identity
            "--bt-request-peer-speed-limit=0",
            "--bt-tracker-connect-timeout=30",  # give slow trackers more time to respond
            "--bt-tracker-timeout=30",
            "--dht-message-timeout=20",
            magnet_link,
        ]
        result = subprocess.run(cmd, timeout=MAX_WAIT_SECONDS)
        if result.returncode != 0:
            print(f"aria2c exited with code {result.returncode}")
            sys.exit(1)
        print("=== DOWNLOAD DONE ===")


def _download_with_size_limit(magnet_link: str) -> None:
    """
    Start aria2c with its RPC interface enabled, and poll RPC for the
    ACTUAL bytes downloaded (completedLength) rather than checking file
    size on disk. This matters because aria2 pre-allocates the full file
    size on disk immediately, so a plain os.path.getsize() check would
    (incorrectly) look "done" before any real data has arrived.
    """
    import time
    import json
    import signal
    import urllib.request

    RPC_PORT = 6800
    RPC_URL = f"http://localhost:{RPC_PORT}/jsonrpc"
    RPC_SECRET = "previewtoken"
    limit_bytes = PREVIEW_MB * 1024 * 1024

    cmd = [
        "aria2c",
        "--enable-rpc",
        f"--rpc-listen-port={RPC_PORT}",
        f"--rpc-secret={RPC_SECRET}",
        "--rpc-listen-all=false",
        "--seed-time=0",
        "--bt-stop-timeout=180",
        "--max-tries=3",
        "--dir", DOWNLOAD_DIR,
        "--console-log-level=warn",
        # --- Peer-discovery resilience flags (see full-download path for why) ---
        "--enable-dht=true",
        "--enable-dht6=false",
        "--dht-listen-port=6881-6999",
        "--bt-enable-lpd=true",
        "--peer-id-prefix=-TR2940-",
        "--bt-request-peer-speed-limit=0",
        "--bt-tracker-connect-timeout=30",
        "--bt-tracker-timeout=30",
        "--dht-message-timeout=20",
        # Force sequential, front-to-back piece downloading. Without this,
        # BitTorrent normally grabs pieces in whatever order is fastest/rarest,
        # which means "10MB downloaded" could be scattered across the middle
        # and end of the file — useless for a watchable preview.
        "--bt-prioritize-piece=head=15M",
        magnet_link,
    ]

    process = subprocess.Popen(cmd)

    def rpc_call(method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "id": "preview",
            "method": method,
            "params": [f"token:{RPC_SECRET}"] + (params or []),
        }
        req = urllib.request.Request(
            RPC_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    # Give aria2c a moment to start its RPC server
    time.sleep(3)

    waited = 0
    while process.poll() is None:
        time.sleep(3)
        waited += 3

        try:
            active = rpc_call("aria2.tellActive")
            results = active.get("result", [])
            total_completed = sum(
                int(item.get("completedLength", 0)) for item in results
            )
            print(f"  Real bytes downloaded so far: {total_completed / (1024*1024):.1f} MB")

            if total_completed >= limit_bytes:
                print(f"Preview size reached ({total_completed / (1024*1024):.1f} MB), stopping aria2c.")
                # Find the file path(s) mid-download before we shut aria2 down
                file_paths = []
                for item in results:
                    for f in item.get("files", []):
                        path = f.get("path")
                        if path:
                            file_paths.append(path)

                try:
                    rpc_call("aria2.shutdown")
                except Exception:
                    pass
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=15)

                # aria2 pre-allocates full file size on disk (sparse file).
                # Truncate down to what was actually downloaded so we don't
                # upload hundreds of MB of empty padding.
                for path in file_paths:
                    if os.path.exists(path):
                        real_size = min(limit_bytes, os.path.getsize(path))
                        with open(path, "r+b") as fh:
                            fh.truncate(real_size)
                        print(f"Truncated {path} to {real_size / (1024*1024):.1f} MB")
                return
        except Exception as e:
            # RPC might not be up yet in the first couple seconds; keep trying
            print(f"  (waiting for aria2 RPC: {e})")

        if waited > 3600:
            print("Preview timeout reached, stopping.")
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=15)
            return

    print(f"aria2c finished on its own (exit code {process.returncode})")


def find_downloaded_files():
    # aria2 also creates .aria2 control files while downloading; ignore those
    all_files = glob.glob(os.path.join(DOWNLOAD_DIR, "**", "*"), recursive=True)
    files = [f for f in all_files if os.path.isfile(f) and not f.endswith(".aria2")]
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
