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
        print(f"PREVIEW MODE: will stop after ~{PREVIEW_MB}MB of REAL downloaded data")
        _download_with_size_limit(magnet_link)
    else:
        print(f"Starting full download for: {magnet_link}")
        cmd = [
            "aria2c",
            "--seed-time=0",
            "--bt-stop-timeout=60",
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
        "--bt-stop-timeout=60",
        "--max-tries=3",
        "--dir", DOWNLOAD_DIR,
        "--console-log-level=warn",
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
