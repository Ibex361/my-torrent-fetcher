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


def download_torrent(magnet_link: str, preview_only: bool = False, client: str = "peerflix", file_indices=None) -> None:
    """
    Downloads a torrent using one of three interchangeable backends,
    selectable via the `client` argument ("peerflix", "qbittorrent", or
    "transmission"). All three write finished file(s) into DOWNLOAD_DIR
    so the rest of the pipeline (compression, Telegram upload) doesn't
    need to know which one ran.

    file_indices: optional set/list of file indices (from list_torrent_files)
    to selectively download instead of the whole torrent. Only supported
    with client="qbittorrent" — peerflix and transmission-cli here always
    grab everything, since selective-file support isn't wired up for them.

    Why multiple options exist: aria2 repeatedly failed to even fetch
    torrent metadata for several magnet links on GitHub's runners (stuck
    at 0 connections, 0 bytes downloaded, indefinitely) despite those same
    torrents having healthy seed counts and downloading instantly from a
    phone/home network. peerflix (pure-JS, no native deps) was confirmed
    to work around this. qBittorrent-nox and transmission-cli are standard,
    mainstream Ubuntu packages included here as additional options to try
    — they may turn out faster than peerflix, but unlike peerflix they
    have not been confirmed against GitHub's specific network behavior
    yet, so treat a first run with either as a live test.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    limit_bytes = PREVIEW_MB * 1024 * 1024 if preview_only else None

    if preview_only:
        print(f"PREVIEW MODE: will stop after ~{PREVIEW_MB}MB of downloaded data")

    print(f"=== DOWNLOADING (client: {client}) === {magnet_link}")

    if file_indices and client != "qbittorrent":
        print(f"NOTE: file_indices selection is only supported with client=qbittorrent; ignoring for {client}.")
        file_indices = None

    if client == "qbittorrent":
        _run_qbittorrent(magnet_link, limit_bytes, file_indices=file_indices)
    elif client == "transmission":
        _run_transmission(magnet_link, limit_bytes)
    else:
        _run_peerflix(magnet_link, limit_bytes)

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


def list_torrent_files(magnet_link: str) -> None:
    """
    Connects to qBittorrent, adds the torrent paused, and prints every
    file in it with an index number — used for the 'list files' mode so
    you can then pick which ones to actually download by number.
    """
    import time
    import json
    import urllib.request
    import urllib.parse

    WEBUI_PORT = 8080
    BASE_URL = f"http://localhost:{WEBUI_PORT}"

    conf_dir = os.path.expanduser("~/.config/qBittorrent")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "qBittorrent.conf"), "w") as f:
        f.write(
            "[Preferences]\n"
            "WebUI\\Enabled=true\n"
            f"WebUI\\Port={WEBUI_PORT}\n"
            "WebUI\\LocalHostAuth=false\n"
            "WebUI\\CSRFProtection=false\n"
            "WebUI\\HostHeaderValidation=false\n"
        )

    daemon = subprocess.Popen(
        ["qbittorrent-nox", "--webui-port=" + str(WEBUI_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(8)

    def api_get(path):
        req = urllib.request.Request(BASE_URL + path)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    try:
        # Add paused, don't actually download anything yet
        boundary = "----qbitboundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"urls\"\r\n\r\n{magnet_link}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"paused\"\r\n\r\ntrue\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            BASE_URL + "/api/v2/torrents/add",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req, timeout=10)
        print("Magnet submitted, waiting for metadata...")

        info_hash = None
        for _ in range(60):  # up to ~2 minutes to get metadata
            time.sleep(2)
            torrents = json.loads(api_get("/api/v2/torrents/info"))
            if torrents:
                info_hash = torrents[0]["hash"]
                if torrents[0].get("total_size", 0) > 0:
                    break

        if not info_hash:
            print("Could not fetch torrent metadata in time (no peers responding?).")
            return

        files_raw = api_get(f"/api/v2/torrents/files?hash={info_hash}")
        files = json.loads(files_raw)

        print("\n" + "=" * 70)
        print("FILES IN THIS TORRENT:")
        print("=" * 70)
        lines = []
        for i, f in enumerate(files):
            size_mb = f["size"] / (1024 * 1024)
            line = f"  [{i}] {f['name']}  ({size_mb:.1f} MB)"
            print(line)
            lines.append(line)
        print("=" * 70)

        send_status(
            "📋 Files found in this torrent:\n\n" + "\n".join(lines) +
            "\n\nRun the workflow again with mode=download and file_indices set "
            "to a comma-separated list of the numbers you want (e.g. \"0,2,5\")."
        )

    finally:
        _stop_process(daemon)
    import signal
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
    except Exception:
        pass


def _run_qbittorrent(magnet_link: str, limit_bytes, file_indices=None) -> None:
    """
    Runs qbittorrent-nox (headless qBittorrent) via its WebUI HTTP API:
    start the daemon, log in, add the magnet link, poll progress, stop
    once done (or once limit_bytes is reached for preview mode), then
    copy the finished file(s) into DOWNLOAD_DIR.

    NOT YET CONFIRMED against GitHub's network behavior the way peerflix
    was — qbittorrent-nox is a standard, well-maintained Ubuntu package,
    so it should install and run correctly, but whether ITS particular
    peer-discovery approach fares better or worse than aria2's on GitHub's
    runners is an open question the first real run will answer.
    """
    import time
    import json
    import shutil
    import urllib.request
    import urllib.parse

    WEBUI_PORT = 8080
    BASE_URL = f"http://localhost:{WEBUI_PORT}"
    qb_download_dir = "/home/runner/.qbittorrent-downloads"
    os.makedirs(qb_download_dir, exist_ok=True)

    # LocalHostAuth=false is qBittorrent's own documented setting for
    # allowing unauthenticated WebUI access from localhost — avoids needing
    # to construct a PBKDF2 password hash by hand, which is easy to get
    # subtly wrong and would silently break login.
    conf_dir = os.path.expanduser("~/.config/qBittorrent")
    os.makedirs(conf_dir, exist_ok=True)
    conf_path = os.path.join(conf_dir, "qBittorrent.conf")
    with open(conf_path, "w") as f:
        f.write(
            "[Preferences]\n"
            "WebUI\\Enabled=true\n"
            f"WebUI\\Port={WEBUI_PORT}\n"
            "WebUI\\LocalHostAuth=false\n"
            "WebUI\\CSRFProtection=false\n"
            "WebUI\\HostHeaderValidation=false\n"
        )

    print("Starting qbittorrent-nox...")
    daemon = subprocess.Popen(
        ["qbittorrent-nox", "--webui-port=" + str(WEBUI_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(8)  # give the WebUI a moment to come up

    def api_post(path, data=None, cookie=None):
        req = urllib.request.Request(
            BASE_URL + path,
            data=urllib.parse.urlencode(data or {}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **({"Cookie": cookie} if cookie else {})},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode(), resp.headers.get("Set-Cookie")

    def api_get(path, cookie):
        req = urllib.request.Request(BASE_URL + path, headers={"Cookie": cookie} if cookie else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()

    cookie = ""
    try:
        # With LocalHostAuth=false, qBittorrent auto-authenticates requests
        # from 127.0.0.1 without needing a real login — but we still try
        # the login endpoint first since it's harmless if already trusted.
        try:
            _, set_cookie = api_post("/api/v2/auth/login", {"username": "admin", "password": "adminadmin"})
            if set_cookie:
                cookie = set_cookie.split(";")[0]
        except Exception:
            pass  # fine — LocalHostAuth=false means we don't strictly need this to succeed
        print("Connected to qBittorrent WebUI.")

        # Add the torrent — paused if we need to select specific files first,
        # since priorities can only be set once qBittorrent has metadata.
        add_paused = "true" if file_indices else "false"
        boundary = "----qbitboundary"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"urls\"\r\n\r\n{magnet_link}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"savepath\"\r\n\r\n{qb_download_dir}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"paused\"\r\n\r\n{add_paused}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            BASE_URL + "/api/v2/torrents/add",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Cookie": cookie},
        )
        urllib.request.urlopen(req, timeout=10)
        print("Magnet link submitted to qBittorrent.")

        info_hash = None
        if file_indices:
            print(f"Waiting for metadata to apply file selection: {file_indices}")
            for _ in range(60):
                time.sleep(2)
                torrents = json.loads(api_get("/api/v2/torrents/info", cookie))
                if torrents and torrents[0].get("total_size", 0) > 0:
                    info_hash = torrents[0]["hash"]
                    break

            if not info_hash:
                print("WARNING: could not get metadata in time to apply file selection; downloading everything instead.")
            else:
                all_files = json.loads(api_get(f"/api/v2/torrents/files?hash={info_hash}", cookie))
                selected = set(file_indices)
                # Set priority 0 (don't download) for everything NOT selected,
                # priority 1 (normal) for everything selected.
                for i in range(len(all_files)):
                    priority = "1" if i in selected else "0"
                    prio_body = urllib.parse.urlencode({
                        "hash": info_hash, "id": str(i), "priority": priority
                    }).encode()
                    req = urllib.request.Request(
                        BASE_URL + "/api/v2/torrents/filePrio",
                        data=prio_body,
                        headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
                    )
                    urllib.request.urlopen(req, timeout=10)
                print(f"Applied file selection: downloading {len(selected)}/{len(all_files)} files.")

                # Now resume the torrent since we added it paused
                resume_body = urllib.parse.urlencode({"hashes": info_hash}).encode()
                req = urllib.request.Request(
                    BASE_URL + "/api/v2/torrents/resume",
                    data=resume_body,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie},
                )
                urllib.request.urlopen(req, timeout=10)

        waited = 0
        last_print = 0.0
        while waited < MAX_WAIT_SECONDS:
            time.sleep(3)
            waited += 3

            try:
                info_raw = api_get("/api/v2/torrents/info", cookie)
                torrents = json.loads(info_raw)
            except Exception as e:
                print(f"  (waiting for qBittorrent API: {e})")
                continue

            if not torrents:
                continue

            t = torrents[0]
            downloaded = t.get("downloaded", 0)
            progress_pct = t.get("progress", 0) * 100
            state = t.get("state", "?")

            now = time.time()
            if now - last_print >= 10:
                last_print = now
                print(f"  [qbittorrent] {progress_pct:.1f}% | {downloaded / (1024*1024):.1f} MB | state={state}")

            if limit_bytes is not None and downloaded >= limit_bytes:
                print(f"Preview size reached ({downloaded / (1024*1024):.1f} MB), stopping.")
                break

            if limit_bytes is None and state in ("uploading", "stalledUP", "queuedUP", "pausedUP", "forcedUP"):
                print("qBittorrent reports the download is complete (now seeding).")
                break

    finally:
        try:
            api_post("/api/v2/app/shutdown", cookie=cookie)
        except Exception:
            pass
        _stop_process(daemon)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(qb_download_dir, "**", "*"), recursive=True):
        if os.path.isfile(f):
            rel = os.path.relpath(f, qb_download_dir)
            dest = os.path.join(DOWNLOAD_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(f, dest)

    if limit_bytes is not None:
        for f in find_downloaded_files():
            if os.path.getsize(f) > limit_bytes:
                with open(f, "r+b") as fh:
                    fh.truncate(limit_bytes)
                print(f"Truncated {f} to {limit_bytes / (1024*1024):.1f} MB for preview.")


def _run_transmission(magnet_link: str, limit_bytes) -> None:
    """
    Runs transmission-daemon + transmission-remote (the CLI control tool)
    to add a magnet link, poll progress, and stop once done or once
    limit_bytes is reached, then copies the finished file(s) into
    DOWNLOAD_DIR.

    NOT YET CONFIRMED against GitHub's network behavior — see the note in
    _run_qbittorrent for why.
    """
    import time
    import shutil
    import re

    tr_download_dir = "/home/runner/.transmission-downloads"
    os.makedirs(tr_download_dir, exist_ok=True)

    print("Starting transmission-daemon...")
    # The apt package can auto-start a system transmission-daemon service;
    # stop it first so it doesn't conflict with our own foreground instance
    # on the same port.
    subprocess.run(["sudo", "systemctl", "stop", "transmission-daemon"], capture_output=True)
    subprocess.run(["sudo", "pkill", "-f", "transmission-daemon"], capture_output=True)
    import time as _time
    _time.sleep(2)

    daemon = subprocess.Popen(
        [
            "transmission-daemon",
            "--foreground",
            "--download-dir", tr_download_dir,
            "--no-auth",
            "--allowed", "127.0.0.1,localhost",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(5)

    try:
        add_result = subprocess.run(
            ["transmission-remote", "localhost", "--add", magnet_link],
            capture_output=True, text=True, timeout=15,
        )
        print(add_result.stdout.strip())
        if add_result.returncode != 0:
            raise RuntimeError(f"Failed to add torrent: {add_result.stderr}")

        waited = 0
        last_print = 0.0
        while waited < MAX_WAIT_SECONDS:
            time.sleep(3)
            waited += 3

            status = subprocess.run(
                ["transmission-remote", "localhost", "--torrent", "all", "--info"],
                capture_output=True, text=True, timeout=15,
            )
            output = status.stdout

            # Parse "Percent Done: NN%" and "Have: X MB" style lines
            pct_match = re.search(r"Percent Done:\s*([\d.]+)%", output)
            have_match = re.search(r"Have:\s*([\d.]+)\s*(MB|GB|KB)", output)
            state_match = re.search(r"State:\s*(.+)", output)

            pct = float(pct_match.group(1)) if pct_match else 0.0
            state = state_match.group(1).strip() if state_match else "?"

            have_bytes = 0
            if have_match:
                val, unit = float(have_match.group(1)), have_match.group(2)
                multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit]
                have_bytes = val * multiplier

            now = time.time()
            if now - last_print >= 10:
                last_print = now
                print(f"  [transmission] {pct:.1f}% | {have_bytes / (1024*1024):.1f} MB | state={state}")

            if limit_bytes is not None and have_bytes >= limit_bytes:
                print(f"Preview size reached ({have_bytes / (1024*1024):.1f} MB), stopping.")
                break

            if limit_bytes is None and pct >= 100.0:
                print("Transmission reports the download is complete.")
                break

    finally:
        try:
            subprocess.run(["transmission-remote", "localhost", "--exit"], capture_output=True, timeout=10)
        except Exception:
            pass
        _stop_process(daemon)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    for f in glob.glob(os.path.join(tr_download_dir, "**", "*"), recursive=True):
        if os.path.isfile(f):
            rel = os.path.relpath(f, tr_download_dir)
            dest = os.path.join(DOWNLOAD_DIR, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(f, dest)

    if limit_bytes is not None:
        for f in find_downloaded_files():
            if os.path.getsize(f) > limit_bytes:
                with open(f, "r+b") as fh:
                    fh.truncate(limit_bytes)
                print(f"Truncated {f} to {limit_bytes / (1024*1024):.1f} MB for preview.")


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
    client = os.environ.get("CLIENT", "peerflix").strip().lower()
    action = os.environ.get("ACTION", "download").strip().lower()  # "download" or "list_files"

    file_indices = None
    raw_indices = os.environ.get("FILE_INDICES", "").strip()
    if raw_indices:
        try:
            file_indices = {int(x.strip()) for x in raw_indices.split(",") if x.strip() != ""}
        except ValueError:
            print(f"Could not parse FILE_INDICES={raw_indices!r}, ignoring (must be comma-separated numbers).")

    if action == "list_files":
        send_status(f"🔎 Listing files in this torrent using qbittorrent...")
        list_torrent_files(magnet_link)
        return

    mode = "PREVIEW" if preview_only else ("FULL + COMPRESS" if compress else "FULL")
    files_note = f"\nDownloading {len(file_indices)} selected file(s)." if file_indices else ""
    send_status(f"🚀 Job started ({mode})\nTool: {client}\nDownloading torrent now...{files_note}")

    download_torrent(magnet_link, preview_only=preview_only, client=client, file_indices=file_indices)
    files = find_downloaded_files()
    print(f"Found {len(files)} file(s) to send.")
    send_status(f"📥 Download complete ({client}) — {len(files)} file(s) found.")

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
